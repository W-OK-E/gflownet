import copy
import os
import signal
import tarfile
import threading
import time
import pandas as pd
import numpy as np
from typing import Tuple, List, Any, Dict

import rdkit.Chem as Chem
from rdkit.Chem import QED
from rdkit import RDLogger

from determined.pytorch import DataLoader, PyTorchTrial, PyTorchTrialContext, LRScheduler
import torch
import torch.multiprocessing as mp
import torch.nn as nn
import torch_geometric.data as gd
import torch_geometric.nn as gnn
from torch_geometric.utils import add_self_loops
from torch.utils.data import Dataset, IterableDataset

from gflownet.envs.graph_building_env import GraphBuildingEnv, GraphActionType, GraphActionCategorical
from gflownet.envs.graph_building_env import generate_forward_trajectory
from gflownet.envs.mol_building_env import MolBuildingEnvContext
from gflownet.algo.trajectory_balance import TrajectoryBalance
from gflownet.tasks.MXMNet import model_standalone as mxmnet
from gflownet.utils.multiprocessing import wrap_model_mp


class QM9Dataset(Dataset):
    def __init__(self, h5_file=None, xyz_file=None, train=True, split_seed=142857, ratio=0.9):
        if h5_file is not None:
            self.df = pd.HDFStore(h5_file, 'r')['df']
        elif xyz_file is not None:
            self.load_tar()
        rng = np.random.default_rng(split_seed)
        idcs = np.arange(len(self.df))
        rng.shuffle(idcs)
        self._min = self.df['gap'].min()
        self._max = self.df['gap'].max()
        self._gap = self._max - self._min
        self._rtrans = 'unit'
        if train:
            self.idcs = idcs[:int(np.floor(ratio * len(self.df)))]
        else:
            self.idcs = idcs[int(np.floor(ratio * len(self.df))):]
            
    def load_tar(self, xyz_file):
        f = tarfile.TarFile(xyz_file, 'r')
        labels = ['rA', 'rB', 'rC', 'mu', 'alpha', 'homo', 'lumo',
                  'gap', 'r2', 'zpve', 'U0', 'U', 'H', 'G', 'Cv']
        all_mols = []
        for pt in f:
            pt = f.extractfile(pt)
            data = pt.read().decode().splitlines()
            all_mols.append(data[-2].split()[:1] + list(map(float, data[1].split()[2:])))
        self.df = pd.DataFrame(all_mols, columns=['SMILES']+labels)

    def reward_transform(self, r):
        if self._rtrans == 'exp':
            return np.exp(-(r - self._min) / self._gap)
        elif self._rtrans == 'unit':
            return 1 - (r - self._min) / (self._gap + 1e-4)

    def inverse_reward_transform(self, rp):
        if self._rtrans == 'exp':
            return -np.log(rp) * self._gap + self._min
        elif self._rtrans == 'unit':
            return (1 - rp) * (self._gap + 1e-4) + self._min

    def __len__(self):
        return len(self.idcs)

    def __getitem__(self, idx):
        return (self.df['SMILES'][self.idcs[idx]], self.reward_transform(self.df['gap'][self.idcs[idx]]))


                    
# TODO: This class could be abstracted? Either made part of
# trajectory_balance or made into a more generic sample-model +
# sample-dataset combinator.
class QM9SamplingIterator(IterableDataset):
    """This class allows us to parallelise and train faster. 

    By separating sampling data/the model and building torch geometric
    graphs from training the model, we can do the former in different
    processes, which is much faster since much of graph construction
    is CPU-bound.

    """
    def __init__(self, qm9_dataset, model, batch_size, ctx, algo, task, ratio=0.5, stream=True):
        """Parameters
        ----------
        qm9_dataset: QM9Dataset
            A dataset instance
        model: nn.Module
            The model we sample from (must be on CUDA already or share_memory() must be called so that
            parameters are synchronized between each worker)
        batch_size: int
            The number of trajectories, each trajectory will be comprised of many graphs, so this is 
            _not_ the batch size in terms of the number of graphs (that will depend on the task)
        algo:
            The training algorithm, e.g. a TrajectoryBalance instance
        task: ConditionalTask
        ratio: float
            The ratio of offline trajectories in the batch.
        stream: bool
            If True, data is sampled iid for every batch. Otherwise, this is a normal in-order
            dataset iterator.

        """
        self._data = qm9_dataset
        self.model = model
        self.batch_size = batch_size
        self.offline_batch_size = int(np.ceil(batch_size * ratio))
        self.online_batch_size = int(np.floor(batch_size * (1 - ratio)))
        self.ratio = ratio
        self.ctx = ctx
        self.algo = algo
        self.task = task
        self.stream = stream

    def _idx_iterator(self):
        RDLogger.DisableLog('rdApp.*')
        if self.stream:
            # If we're streaming data, just sample `offline_batch_size` indices
            while True:
                yield self.rng.integers(0, len(self._data.idcs), self.offline_batch_size)
        else:
            # Otherwise, figure out which indices correspond to this worker
            worker_info = torch.utils.data.get_worker_info()
            n = len(self._data.idcs)
            if worker_info is None:
                start, end, wid = 0, n, -1
            else:
                nw = worker_info.num_workers
                wid = worker_info.id
                start, end = int(np.floor(n / nw * wid)), int(np.ceil(n / nw * (wid+1)))
            bs = self.offline_batch_size
            if end - start < bs:
                yield np.arange(start, end)
                return
            for i in range(start, end - bs, bs):
                yield np.arange(i, i + bs)
            if i + bs < end:
                yield np.arange(i + bs, end)

    def __len__(self):
        if self.stream:
            return int(1e6)
        return len(self._data.idcs)

    def __iter__(self):
        worker_info = torch.utils.data.get_worker_info()
        wid = (worker_info.id if worker_info is not None else 0)
        self.rng = self.algo.rng = self.task.rng = np.random.default_rng(142857 + wid)
        for idcs in self._idx_iterator():
            num_offline = idcs.shape[0] # This is in [1, self.offline_batch_size]
            # Sample conditional info such as temperature, trade-off weights, etc.
            cond_info = self.task.sample_conditional_information(num_offline + self.online_batch_size)
            is_valid = torch.ones(cond_info.shape[0]).bool()
            
            # Sample some dataset data
            smiles, flat_rewards = map(list, zip(*[self._data[i] for i in idcs]))
            graphs = [self.ctx.mol_to_graph(Chem.MolFromSmiles(s)) for s in smiles]
            trajs = self.algo.create_training_data_from_graphs(graphs)
            # Sample some on-policy data
            if self.online_batch_size > 0:
                with torch.no_grad():
                    trajs += self.algo.create_training_data_from_own_samples(
                        self.model, self.online_batch_size, cond_info[num_offline:])
                if self.algo.bootstrap_own_reward:
                    # The model can be trained to predict its own reward,
                    # i.e. predict the output of cond_info_to_reward
                    pred_reward = [i['reward_pred'].cpu().item() for i in trajs[num_offline:]]
                    raise ValueError('make this flat rewards')
                else:
                    # Otherwise
                    valid_idcs = torch.tensor([i+num_offline for i in range(self.online_batch_size)
                                               if trajs[i+num_offline]['is_valid']]).long()
                    pred_reward = torch.zeros((self.online_batch_size))
                    # fetch the valid trajectories endpoints
                    mols = [self.ctx.graph_to_mol(trajs[i]['traj'][-1][0]) for i in valid_idcs]
                    # ask the task to compute their reward
                    preds, m_is_valid = self.task.compute_flat_rewards(mols)
                    # The task may decide some of the mols are invalid, we have to again filter those
                    valid_idcs = valid_idcs[m_is_valid]
                    pred_reward[valid_idcs - num_offline] = preds
                    is_valid[num_offline:] = False
                    is_valid[valid_idcs] = True
                    flat_rewards += list(pred_reward)
                    # Override the is_valid key in case the task made some mols invalid
                    for i in range(self.online_batch_size):
                        trajs[num_offline + i]['is_valid'] = is_valid[num_offline + i].item()
            # Compute scalar rewards from conditional information & flat rewards
            rewards = self.task.cond_info_to_reward(cond_info, flat_rewards)
            rewards[torch.logical_not(is_valid)] = np.exp(self.algo.illegal_action_logreward)
            # Construct batch
            batch = self.algo.construct_batch(trajs, cond_info, rewards, self.model.action_type_order)
            batch.smiles = smiles
            # TODO: There is a smarter way to do this
            # batch.pin_memory()
            yield batch

def mlp(n_in, n_hid, n_out, n_layer, act=nn.LeakyReLU):
    n = [n_in] + [n_hid] * n_layer + [n_out]
    return nn.Sequential(*sum([[nn.Linear(n[i], n[i+1]), act()] for i in range(n_layer + 1)], [])[:-1])
    
class Model(nn.Module):
    def __init__(self, env_ctx, num_emb=64, num_layers=3, num_heads=2):
        super().__init__()
        self.num_layers = num_layers
        
        self.x2h = mlp(env_ctx.num_node_dim, num_emb, num_emb, 2)
        self.e2h = mlp(env_ctx.num_edge_dim, num_emb, num_emb, 2)
        self.c2h = mlp(env_ctx.num_cond_dim, num_emb, num_emb, 2)
        self.graph2emb = nn.ModuleList(
            sum([[
                gnn.GENConv(num_emb, num_emb, num_layers=1, aggr='add', norm=None),
                gnn.TransformerConv(num_emb * 2, num_emb, edge_dim=num_emb, heads=num_heads),
                nn.Linear(num_heads * num_emb, num_emb),
                gnn.LayerNorm(num_emb, affine=False),
                mlp(num_emb, num_emb * 4, num_emb, 1),
                gnn.LayerNorm(num_emb, affine=False),
            ] for i in range(self.num_layers)], []))
        num_final = num_emb * 2
        num_mlp_layers = 0
        self.emb2add_edge = mlp(num_final, num_emb, 1, num_mlp_layers)
        self.emb2add_node = mlp(num_final, num_emb, env_ctx.num_new_node_values, num_mlp_layers)
        self.emb2set_node_attr = mlp(num_final, num_emb, env_ctx.num_node_attr_logits, num_mlp_layers)
        self.emb2set_edge_attr = mlp(num_final, num_emb, env_ctx.num_edge_attr_logits, num_mlp_layers)
        self.emb2stop = mlp(num_emb * 3, num_emb, 1, num_mlp_layers)
        self.emb2reward = mlp(num_emb * 3, num_emb, 1, num_mlp_layers)
        self.logZ = mlp(env_ctx.num_cond_dim, num_emb * 2, 1, 2)
        self.action_type_order = [
            GraphActionType.Stop,
            GraphActionType.AddNode,
            GraphActionType.SetNodeAttr,
            GraphActionType.AddEdge,
            GraphActionType.SetEdgeAttr
        ]


    def forward(self, g: gd.Batch, cond: torch.tensor):
        o = self.x2h(g.x)
        e = self.e2h(g.edge_attr)
        c = self.c2h(cond)
        num_total_nodes = g.x.shape[0]
        # Augment the edges with a new edge to the conditioning
        # information node. This new node is connected to every node
        # within its graph.
        u, v = torch.arange(num_total_nodes, device=o.device), g.batch + num_total_nodes
        aug_edge_index = torch.cat(
            [g.edge_index,
             torch.stack([u, v]),
             torch.stack([v, u])],
            1)
        e_p = torch.zeros((num_total_nodes * 2, e.shape[1]), device=g.x.device)
        e_p[:, 0] = 1 # Manually create a bias term
        aug_e = torch.cat([e, e_p], 0)
        aug_edge_index, aug_e = add_self_loops(aug_edge_index, aug_e, 'mean')
        aug_batch = torch.cat([g.batch, torch.arange(c.shape[0], device=o.device)], 0)
            
        # Append the conditioning information node embedding to o
        o_0 = o = torch.cat([o, c], 0)
        for i in range(self.num_layers):
            # Run the graph transformer forward
            gen, trans, linear, norm1, ff, norm2 = self.graph2emb[i * 6: (i+1) * 6]
            agg = gen(o, aug_edge_index, aug_e)
            o = norm1(o + linear(trans(torch.cat([o, agg], 1), aug_edge_index, aug_e)), aug_batch)
            o = norm2(o + ff(o), aug_batch)
            
        glob = torch.cat([gnn.global_mean_pool(o[:-c.shape[0]], g.batch), o[-c.shape[0]:], c], 1)
        o_final = torch.cat([o[:-c.shape[0]], c[g.batch]], 1)
        
        ne_row, ne_col = g.non_edge_index
        # On `::2`, edges are duplicated to make graphs undirected, only take the even ones
        e_row, e_col = g.edge_index[:, ::2]
        cat = GraphActionCategorical(
            g,
            logits=[
                self.emb2stop(glob),
                self.emb2add_node(o_final),
                self.emb2set_node_attr(o_final),
                self.emb2add_edge(o_final[ne_row] + o_final[ne_col]),
                self.emb2set_edge_attr(o_final[e_row] + o_final[e_col]),
            ],
            keys=[None, 'x', 'x', 'non_edge_index', 'edge_index'],
            types=self.action_type_order,
        )
        return cat, self.emb2reward(glob)

class ConditionalTask:
    """This class captures conditional information generation and reward transforms"""
    
    def __init__(self, dataset, temperature_distribution, temperature_parameters, wrap_model=None):
        self._wrap_model = wrap_model if wrap_model is not None else lambda x:x
        self.models = self.load_task_models()
        self.dataset = dataset
        self.temperature_sample_dist = temperature_distribution
        self.temperature_dist_params = temperature_parameters

    def load_task_models(self):
        gap_model = mxmnet.MXMNet(mxmnet.Config(128, 6, 5.0))
        gap_model.device = torch.device('cuda')
        # TODO: this path should be part of the config?
        state_dict = torch.load('/data/chem/qm9/mxmnet_gap_model.pt')
        gap_model.load_state_dict(state_dict)
        gap_model.cuda()
        gap_model = self._wrap_model(gap_model)
        return {'mxmnet_gap': gap_model}

    def sample_conditional_information(self, n):
        beta = None
        if self.temperature_sample_dist == 'gamma':
            beta = self.rng.gamma(*self.temperature_dist_params, n).astype(np.float32)
        elif self.temperature_sample_dist == 'uniform':
            beta = self.rng.uniform(*self.temperature_dist_params, n).astype(np.float32)
        elif self.temperature_sample_dist == 'beta':
            beta = self.rng.beta(*self.temperature_dist_params, n).astype(np.float32)
        return torch.tensor(beta).reshape((-1, 1))

    def cond_info_to_reward(self, cond_info, flat_reward):
        if isinstance(flat_reward, list):
            flat_reward = torch.tensor(flat_reward)
        return flat_reward ** cond_info[:, 0]

    def compute_flat_rewards(self, mols):
        graphs = [mxmnet.mol2graph(i) for i in mols]
        is_valid = torch.tensor([i is not None for i in graphs]).bool()
        if not is_valid.any():
            return torch.zeros((0,)), is_valid
        batch = gd.Batch.from_data_list([i for i in graphs if i is not None])
        batch.to(self.models['mxmnet_gap'].device)
        preds = self.models['mxmnet_gap'](batch).reshape((-1,)).data.cpu() / mxmnet.HAR2EV
        preds[preds.isnan()] = 1
        preds = self.dataset.reward_transform(preds).clip(1e-4, 2)
        return preds, is_valid
    
class QM9Trial(PyTorchTrial):
    def __init__(self, context: PyTorchTrialContext) -> None:
        self.context = context
        default_hps = {
            # TODO: write down default hyperparameters to reduce pollution in config files
        }
        hps = {
            # This c = {**a, **b} notation overrides a[k] with b[k] in
            # c if k is a key of both dicts
            **default_hps,
            **context.get_hparams(), 
        }
        
        self.num_workers = context.get_hparam('num_data_loader_workers')
        RDLogger.DisableLog('rdApp.*')
        self.rng = np.random.default_rng(142857)
        self.env = GraphBuildingEnv()
        self.ctx = MolBuildingEnvContext(['H', 'C', 'N', 'F', 'O'], num_cond_dim=1)
        self.training_data = QM9Dataset(self.context.get_data_config()['h5_path'], train=True)
        
        model = Model(self.ctx,
                      num_emb=context.get_hparam('num_emb'),
                      num_layers=context.get_hparam('num_layers'))
        self.model = context.wrap_model(model)
        self.model.device = torch.device('cuda')
        
        self.sampling_tau = context.get_hparam('sampling_tau')
        if self.sampling_tau > 0:
            self.sampling_model = context.wrap_model(copy.deepcopy(model))
            self.sampling_model.device = torch.device('cuda')
        else:
            self.sampling_model = self.model
            
        # Separate Z parameters from non-Z to allow for LR decay on the former
        Z_params = list(model.logZ.parameters())
        non_Z_params = [i for i in self.model.parameters() if all(id(i) != id(j) for j in Z_params)]
        self.opt = context.wrap_optimizer(
            torch.optim.Adam(non_Z_params,
                             context.get_hparam('learning_rate'),
                             (context.get_hparam('momentum'), 0.999),
                             weight_decay=context.get_hparam('weight_decay'),
                             eps=context.get_hparam('adam_eps')))
        
        self.opt_Z = context.wrap_optimizer(
            torch.optim.Adam(Z_params, context.get_hparam('learning_rate'),
                             (0.9,0.999)))
        self.lr_sched = self.context.wrap_lr_scheduler(
            torch.optim.lr_scheduler.LambdaLR(
                self.opt, lambda steps: 2 ** (-steps / context.get_hparam('lr_decay'))),
            LRScheduler.StepMode.STEP_EVERY_BATCH)
        self.Z_lr_sched = self.context.wrap_lr_scheduler(
            torch.optim.lr_scheduler.LambdaLR(
                self.opt_Z, lambda steps: 2 ** (-steps / context.get_hparam('Z_lr_decay'))),
            LRScheduler.StepMode.STEP_EVERY_BATCH)

        eps = context.get_hparam('tb_epsilon')
        self.algo = TrajectoryBalance(self.env, self.ctx, self.rng,
                                      random_action_prob=context.get_hparam('random_action_prob'), 
                                      max_nodes=9,
                                      illegal_action_logreward=context.get_hparam('illegal_action_logreward'),
                                      epsilon=eval(eps) if isinstance(eps, str) else eps)
        self.algo.reward_loss_multiplier = context.get_hparam('reward_loss_multiplier')
        self.algo.bootstrap_own_reward = context.get_hparam('bootstrap_own_reward')

        self.task = ConditionalTask(self.training_data,
                                    context.get_hparam('temperature_sample_dist'),
                                    eval(context.get_hparam('temperature_dist_params')),
                                    wrap_model=self._wrap_model_mp)
        self.mb_size = self.context.get_per_slot_batch_size()
        self.clip_grad_param = context.get_hparam('clip_grad_param')
        self.clip_grad_callback = {
            'value': (lambda params: torch.nn.utils.clip_grad_value_(params, self.clip_grad_param)),
            'norm': (lambda params: torch.nn.utils.clip_grad_norm_(params, self.clip_grad_param)),
            'none': (lambda x: None)
        }[context.get_hparam('clip_grad_type')]
        # See https://docs.determined.ai/latest/training-apis/api-pytorch-advanced.html#customizing-a-reproducible-dataset
        if isinstance(context, PyTorchTrialContext):
            context.experimental.disable_dataset_reproducibility_checks()
            
    def get_batch_length(self, batch):
        return batch.traj_lens.shape[0]

    def _wrap_model_mp(self, model):
        if self.num_workers > 0:
            placeholder = wrap_model_mp(model, self.num_workers, cast_types=(gd.Batch, GraphActionCategorical))
            placeholder.action_type_order = self.model.action_type_order
            return placeholder
        return model
    
    def build_training_data_loader(self) -> DataLoader:
        model = self._wrap_model_mp(self.sampling_model)
        iterator = QM9SamplingIterator(self.training_data, model, self.mb_size * 2, self.ctx, self.algo, self.task)
        return torch.utils.data.DataLoader(iterator, batch_size=None, num_workers=self.num_workers, persistent_workers=self.num_workers > 0)
    
    def build_validation_data_loader(self) -> DataLoader:
        data = QM9Dataset(self.context.get_data_config()['h5_path'], train=False)
        model = self._wrap_model_mp(self.model)
        iterator = QM9SamplingIterator(data, model, self.mb_size, self.ctx, self.algo, self.task,
                                       ratio=1, stream=False)
        return torch.utils.data.DataLoader(iterator, batch_size=None, num_workers=self.num_workers, persistent_workers=self.num_workers > 0)

    def train_batch(self, batch: gd.Batch, epoch_idx: int, batch_idx: int) -> Dict[str, torch.Tensor]:
        if not hasattr(self.model, 'device'):
            self.model.device = self.context.to_device(torch.ones(1)).device
        losses, info = self.algo.compute_batch_losses(self.model, batch, num_bootstrap=self.mb_size)
        avg_offline_loss = losses[:self.mb_size].mean()
        avg_online_loss = losses[self.mb_size:].mean()
        reward_losses = info.get('reward_losses', torch.zeros(1)).mean()
        loss = losses.mean() + reward_losses * self.algo.reward_loss_multiplier
        
        self.context.backward(loss)
        self.context.step_optimizer(self.opt, clip_grads=self.clip_grad_callback)
        self.context.step_optimizer(self.opt_Z, clip_grads=self.clip_grad_callback)
        
        # This isn't wrapped in self.context, would probably break the Trial API
        # TODO: fix, in the event of multi-gpu improvements
        if self.sampling_tau > 0:
            for a, b in zip(self.model.parameters(), self.sampling_model.parameters()):
                b.data.mul_(self.sampling_tau).add_(a.data * (1-self.sampling_tau))
                
        return {'loss': loss.item(),
                'avg_online_loss': avg_online_loss.item(),
                'avg_offline_loss': avg_offline_loss.item(),
                'reward_loss': reward_losses.item(),
                'logZ': info['logZ'],
                'invalid_trajectories': info['invalid_trajectories'].item(),
                'invalid_logprob': info['invalid_logprob'].item(),
                'invalid_losses': info['invalid_losses'].item(),
            }

    def evaluate_batch(self, batch: Tuple[List[str], torch.Tensor]) -> Dict[str, Any]:
        if not hasattr(self.model, 'device'):
            self.model.device = self.context.to_device(torch.ones(1)).device
        losses, info = self.algo.compute_batch_losses(
            self.model, batch, num_bootstrap=len(batch.smiles))
        loss = losses.mean()
        reward_losses = info.get('reward_losses', torch.zeros(1)).mean()
        return {'validation_loss': loss,
                'reward_loss': reward_losses.item(),
                'unnorm_traj_losses': info['unnorm_traj_losses'].mean().item()}

class DummyContext:
    """A Dummy context if we want to run this experiment without Determined"""

    def __init__(self, hps, device):
        self.hps = hps
        self.dev = device
    
    def wrap_model(self, model):
        self.model = model
        return model.to(self.dev)

    def wrap_lr_scheduler(self, sc, *a, **kw):
        # TODO: step schedulers
        return sc
    
    def wrap_optimizer(self, opt):
        return opt

    def wrap_lr_scheduler(self, sc, *a, **kw):
        return sc

    def get_hparams(self):
        return self.hps
    
    def get_hparam(self, hp):
        return self.hps[hp]

    def get_data_config(self):
        return {'h5_path': '/data/chem/qm9/qm9.h5'}

    def get_per_slot_batch_size(self):
        return self.hps['global_batch_size']

    def to_device(self, x):
        return x.to(self.dev)

    def backward(self, loss):
        loss.backward()

    def step_optimizer(self, opt, clip_grads=None):
        if clip_grads is not None:
            [clip_grads(i) for i in self.model.parameters()]
        opt.step()
        opt.zero_grad()
    
def main():
    """Example of how this model can be run outside of Determined"""
    hps = {
        'bootstrap_own_reward': False,
        'learning_rate': 1e-4,
        'global_batch_size': 32,
        'num_emb': 128,
        'num_layers': 3,
        'tb_epsilon': None,
        'illegal_action_logreward': -50,
        'reward_loss_multiplier': 1,
        'temperature_sample_dist': 'uniform',
        'temperature_dist_params': '(.5, 32)',
        'weight_decay': 1e-8,
        'num_data_loader_workers': 8,
        'momentum': 0.9,
        'adam_eps': 1e-8,
        'lr_decay': 10000,
        'Z_lr_decay': 10000,
        'clip_grad_type': 'norm',
        'clip_grad_param': 10,
        'random_action_prob': .001,
        'sampling_tau': 0.99,
    }
    dummy_context = DummyContext(hps, torch.device('cuda'))
    trial = QM9Trial(dummy_context)

    train_dl = trial.build_training_data_loader()
    valid_dl = trial.build_validation_data_loader()
    
    for epoch in range(10):
        for it, batch in enumerate(train_dl):
            batch = batch.to(dummy_context.dev, non_blocking=True)
            r = trial.train_batch(batch, epoch, it)
            print(it, ' '.join(f"{k}: {v:.4f}" for k, v in r.items()))
            if not it % 200:
                torch.save({'models_state_dict': [trial.model.state_dict()], 'hps': hps},
                           open(f'../model_state.pt', 'wb'))
            if it == 10000: # train_dl is an infinite iterator 
                break
        # Somewhere, use valid_dl to
        # trial.evaluate_batch(batch)

if __name__ == '__main__':
    main()