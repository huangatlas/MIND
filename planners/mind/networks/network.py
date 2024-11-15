import math
import torch
import numpy as np
from torch import Tensor, nn
from torch.nn import functional as F
from torch.nn import MultiheadAttention, TransformerEncoderLayer, TransformerEncoder
from typing import Dict, List, Tuple, Optional
from planners.mind.utils import gpu
from planners.mind.networks.layers import Conv1d, Res1d


class ActorNet(nn.Module):
    """
    Actor feature extractor with Conv1D
    """

    def __init__(self, n_in=3, hidden_size=128, n_fpn_scale=4):
        super(ActorNet, self).__init__()
        norm = "GN"
        ng = 1

        n_out = [2 ** (5 + s) for s in range(n_fpn_scale)]  # [32, 64, 128]
        blocks = [Res1d] * n_fpn_scale
        num_blocks = [2] * n_fpn_scale

        groups = []
        for i in range(len(num_blocks)):
            group = []
            if i == 0:
                group.append(blocks[i](n_in, n_out[i], norm=norm, ng=ng))
            else:
                group.append(blocks[i](n_in, n_out[i], stride=2, norm=norm, ng=ng))

            for j in range(1, num_blocks[i]):
                group.append(blocks[i](n_out[i], n_out[i], norm=norm, ng=ng))
            groups.append(nn.Sequential(*group))
            n_in = n_out[i]
        self.groups = nn.ModuleList(groups)

        lateral = []
        for i in range(len(n_out)):
            lateral.append(Conv1d(n_out[i], hidden_size, norm=norm, ng=ng, act=False))
        self.lateral = nn.ModuleList(lateral)

        self.output = Res1d(hidden_size, hidden_size, norm=norm, ng=ng)

    def forward(self, actors: Tensor) -> Tensor:
        out = actors

        outputs = []
        for i in range(len(self.groups)):
            out = self.groups[i](out)
            outputs.append(out)

        out = self.lateral[-1](outputs[-1])
        for i in range(len(outputs) - 2, -1, -1):
            out = F.interpolate(out, scale_factor=2, mode="linear", align_corners=False)
            out += self.lateral[i](outputs[i])

        out = self.output(out)[:, :, -1]
        return out


class PointAggregateBlock(nn.Module):
    def __init__(self, hidden_size: int, aggre_out: bool, dropout: float = 0.1) -> None:
        super(PointAggregateBlock, self).__init__()
        self.aggre_out = aggre_out

        self.fc1 = nn.Sequential(
            nn.Linear(hidden_size, hidden_size),
            nn.LayerNorm(hidden_size),
            nn.ReLU(inplace=True),
            nn.Linear(hidden_size, hidden_size),
            nn.LayerNorm(hidden_size),
            nn.ReLU(inplace=True)
        )
        self.fc2 = nn.Sequential(
            nn.Linear(hidden_size * 2, hidden_size),
            nn.LayerNorm(hidden_size),
            nn.ReLU(inplace=True),
            nn.Linear(hidden_size, hidden_size),
            nn.LayerNorm(hidden_size),
            nn.ReLU(inplace=True)
        )
        self.norm = nn.LayerNorm(hidden_size)

    def _global_maxpool_aggre(self, feat):
        return F.adaptive_max_pool1d(feat.permute(0, 2, 1), 1).permute(0, 2, 1)

    def forward(self, x_inp):
        x = self.fc1(x_inp)  # [N_{lane}, 10, hidden_size]
        x_aggre = self._global_maxpool_aggre(x)
        x_aggre = torch.cat([x, x_aggre.repeat([1, x.shape[1], 1])], dim=-1)

        out = self.norm(x_inp + self.fc2(x_aggre))
        if self.aggre_out:
            return self._global_maxpool_aggre(out).squeeze()
        else:
            return out


class LaneNet(nn.Module):
    def __init__(self, device, in_size=10, hidden_size=128, dropout=0.1):
        super(LaneNet, self).__init__()

        self.device = device

        self.proj = nn.Sequential(
            nn.Linear(in_size, hidden_size),
            nn.LayerNorm(hidden_size),
            nn.ReLU(inplace=True)
        )
        self.aggre1 = PointAggregateBlock(hidden_size=hidden_size, aggre_out=False, dropout=dropout)
        self.aggre2 = PointAggregateBlock(hidden_size=hidden_size, aggre_out=True, dropout=dropout)

    # for av2
    def forward(self, feats):
        x = self.proj(feats)  # [N_{lane}, 10, hidden_size]
        x = self.aggre1(x)
        x = self.aggre2(x)  # [N_{lane}, hidden_size]
        return x


class RelaFusionLayer(nn.Module):
    def __init__(self,
                 device,
                 d_edge: int = 128,
                 d_model: int = 128,
                 d_ffn: int = 2048,
                 n_head: int = 8,
                 dropout: float = 0.1,
                 update_edge: bool = True) -> None:
        super(RelaFusionLayer, self).__init__()
        self.device = device
        self.update_edge = update_edge

        self.proj_memory = nn.Sequential(
            nn.Linear(d_model + d_model + d_edge, d_model),
            nn.LayerNorm(d_model),
            nn.ReLU(inplace=True)
        )

        if self.update_edge:
            self.proj_edge = nn.Sequential(
                nn.Linear(d_model, d_edge),
                nn.LayerNorm(d_edge),
                nn.ReLU(inplace=True)
            )
            self.norm_edge = nn.LayerNorm(d_edge)

        self.multihead_attn = MultiheadAttention(
            embed_dim=d_model, num_heads=n_head, dropout=dropout, batch_first=False)

        # Feedforward model
        self.linear1 = nn.Linear(d_model, d_ffn)
        self.dropout = nn.Dropout(dropout)
        self.linear2 = nn.Linear(d_ffn, d_model)

        self.norm2 = nn.LayerNorm(d_model)
        self.norm3 = nn.LayerNorm(d_model)
        self.dropout2 = nn.Dropout(dropout)
        self.dropout3 = nn.Dropout(dropout)
        self.activation = nn.ReLU(inplace=True)

    def forward(self,
                node: Tensor,
                edge: Tensor,
                edge_mask: Optional[Tensor]) -> Tensor:
        '''
            input:
                node:       (N, d_model)
                edge:       (N, N, d_model)
                edge_mask:  (N, N)
        '''
        # update node
        x, edge, memory = self._build_memory(node, edge)
        x_prime, _ = self._mha_block(x, memory, attn_mask=None, key_padding_mask=edge_mask)
        x = self.norm2(x + x_prime).squeeze()
        x = self.norm3(x + self._ff_block(x))
        return x, edge, None

    def _build_memory(self,
                      node: Tensor,
                      edge: Tensor) -> Tuple[Tensor, Tensor, Tensor]:
        '''
            input:
                node:   (N, d_model)
                edge:   (N, N, d_edge)
            output:
                :param  (1, N, d_model)
                :param  (N, N, d_edge)
                :param  (N, N, d_model)
        '''
        n_token = node.shape[0]

        # 1. build memory
        src_x = node.unsqueeze(dim=0).repeat([n_token, 1, 1])  # (N, N, d_model)
        tar_x = node.unsqueeze(dim=1).repeat([1, n_token, 1])  # (N, N, d_model)
        memory = self.proj_memory(torch.cat([edge, src_x, tar_x], dim=-1))  # (N, N, d_model)
        # 2. (optional) update edge (with residual)
        if self.update_edge:
            edge = self.norm_edge(edge + self.proj_edge(memory))  # (N, N, d_edge)

        return node.unsqueeze(dim=0), edge, memory

    # multihead attention block
    def _mha_block(self,
                   x: Tensor,
                   mem: Tensor,
                   attn_mask: Optional[Tensor],
                   key_padding_mask: Optional[Tensor]) -> Tensor:
        '''
            input:
                x:                  [1, N, d_model]
                mem:                [N, N, d_model]
                attn_mask:          [N, N]
                key_padding_mask:   [N, N]
            output:
                :param      [1, N, d_model]
                :param      [N, N]
        '''
        x, _ = self.multihead_attn(x, mem, mem,
                                   attn_mask=attn_mask,
                                   key_padding_mask=key_padding_mask,
                                   need_weights=False)  # return average attention weights
        return self.dropout2(x), None

    # feed forward block
    def _ff_block(self,
                  x: Tensor) -> Tensor:
        x = self.linear2(self.dropout(self.activation(self.linear1(x))))
        return self.dropout3(x)


class RelaFusionNet(nn.Module):
    def __init__(self,
                 device,
                 d_model: int = 128,
                 d_edge: int = 128,
                 n_head: int = 8,
                 n_layer: int = 6,
                 dropout: float = 0.1,
                 update_edge: bool = True):
        super(RelaFusionNet, self).__init__()
        self.device = device

        fusion = []
        for i in range(n_layer):
            need_update_edge = False if i == n_layer - 1 else update_edge
            fusion.append(RelaFusionLayer(device=device,
                                          d_edge=d_edge,
                                          d_model=d_model,
                                          d_ffn=d_model * 2,
                                          n_head=n_head,
                                          dropout=dropout,
                                          update_edge=need_update_edge))
        self.fusion = nn.ModuleList(fusion)

    def forward(self, x: Tensor, edge: Tensor, edge_mask: Tensor) -> Tensor:
        '''
            x: (N, d_model)
            edge: (d_model, N, N)
            edge_mask: (N, N)
        '''
        # attn_multilayer = []
        for mod in self.fusion:
            x, edge, _ = mod(x, edge, edge_mask)
        return x, None


class FusionNet(nn.Module):
    def __init__(self, device, config):
        super(FusionNet, self).__init__()
        self.device = device

        self.d_embed = config['d_embed']
        self.d_rpe = config['d_rpe']
        self.d_model = config['d_embed']
        dropout = config['dropout']
        update_edge = config['update_edge']

        self.proj_actor = nn.Sequential(
            nn.Linear(config['d_actor'], self.d_model),
            nn.LayerNorm(self.d_model),
            nn.ReLU(inplace=True)
        )
        self.proj_lane = nn.Sequential(
            nn.Linear(config['d_lane'], self.d_model),
            nn.LayerNorm(self.d_model),
            nn.ReLU(inplace=True)
        )
        self.proj_rpe_scene = nn.Sequential(
            nn.Linear(config['d_rpe_in'], config['d_rpe']),
            nn.LayerNorm(config['d_rpe']),
            nn.ReLU(inplace=True)
        )

        self.fuse_scene = RelaFusionNet(self.device,
                                        d_model=self.d_model,
                                        d_edge=config['d_rpe'],
                                        n_head=config['n_scene_head'],
                                        n_layer=config['n_scene_layer'],
                                        dropout=dropout,
                                        update_edge=update_edge)

    def forward(self,
                actors: Tensor,
                actor_idcs: List[Tensor],
                lanes: Tensor,
                lane_idcs: List[Tensor],
                rpe_prep: Dict[str, Tensor]):
        # projection
        actors = self.proj_actor(actors)
        lanes = self.proj_lane(lanes)

        actors_new, lanes_new, cls_new = list(), list(), list()

        for a_idcs, l_idcs, rpes in zip(actor_idcs, lane_idcs, rpe_prep):
            # * fusion - scene
            _actors = actors[a_idcs]
            _lanes = lanes[l_idcs]
            tokens = torch.cat([_actors, _lanes], dim=0)
            cls_token = torch.zeros((1, self.d_model), device=self.device)
            tokens_with_cls = torch.cat([tokens, cls_token], dim=0)

            rpe = self.proj_rpe_scene(rpes['scene'].permute(1, 2, 0))
            rpe_with_cls = torch.zeros(
                (tokens_with_cls.shape[0], tokens_with_cls.shape[0], self.d_rpe),
                device=self.device)
            rpe_with_cls[:tokens.shape[0], :tokens.shape[0], :] = rpe

            out, _ = self.fuse_scene(tokens_with_cls, rpe_with_cls, edge_mask=None)

            actors_new.append(out[:len(a_idcs)])
            lanes_new.append(out[len(a_idcs):-1])
            cls_new.append(out[-1].unsqueeze(0))
        actors = torch.cat(actors_new, dim=0)
        lanes = torch.cat(lanes_new, dim=0)
        cls = torch.cat(cls_new, dim=0)
        return actors, lanes, cls


class SceneDecoder(nn.Module):
    def __init__(self,
                 device,
                 param_out='none',
                 hidden_size=128,
                 future_steps=30,
                 num_modes=6) -> None:
        super(SceneDecoder, self).__init__()
        self.hidden_size = hidden_size
        self.future_steps = future_steps
        self.num_modes = num_modes
        self.device = device
        self.param_out = param_out

        dim_mm = self.hidden_size * num_modes
        dim_inter = dim_mm // 2
        self.actor_proj = nn.Sequential(
            nn.Linear(self.hidden_size, dim_inter),
            nn.LayerNorm(dim_inter),
            nn.ReLU(inplace=True),
            nn.Linear(dim_inter, dim_mm),
            nn.LayerNorm(dim_mm),
            nn.ReLU(inplace=True)
        )

        self.ctx_proj = nn.Sequential(
            nn.Linear(self.hidden_size, dim_inter),
            nn.LayerNorm(dim_inter),
            nn.ReLU(inplace=True),
            nn.Linear(dim_inter, dim_mm),
            nn.LayerNorm(dim_mm),
            nn.ReLU(inplace=True)
        )

        # several layers of transformer encoder
        enc_layer = TransformerEncoderLayer(d_model=self.hidden_size,
                                            nhead=4, dim_feedforward=self.hidden_size * 12)
        self.ctx_sat = TransformerEncoder(enc_layer, num_layers=2, enable_nested_tensor=False)

        # linear projection for rpe embedding rpe_dim = 11
        self.proj_rpe = nn.Sequential(
            nn.Linear(5 * 2 * 2, self.hidden_size),
            nn.LayerNorm(self.hidden_size),
            nn.ReLU(inplace=True)
        )

        self.proj_tgt = nn.Sequential(
            nn.Linear(2 * self.hidden_size, self.hidden_size),
            nn.LayerNorm(self.hidden_size),
            nn.ReLU(inplace=True),
            nn.Linear(self.hidden_size, self.hidden_size),
            nn.LayerNorm(self.hidden_size),
            nn.ReLU(inplace=True)
        )

        self.cls = nn.Sequential(
            nn.Linear(self.hidden_size, self.hidden_size),
            nn.LayerNorm(self.hidden_size),
            nn.ReLU(inplace=True),
            nn.Linear(self.hidden_size, self.hidden_size),
            nn.LayerNorm(self.hidden_size),
            nn.ReLU(inplace=True),
            nn.Linear(self.hidden_size, 1)
        )

        if self.param_out == 'bezier':
            self.N_ORDER = 7
            self.mat_T = self._get_T_matrix_bezier(n_order=self.N_ORDER, n_step=future_steps).to(self.device)
            self.mat_Tp = self._get_Tp_matrix_bezier(n_order=self.N_ORDER, n_step=future_steps).to(self.device)

            self.reg = nn.Sequential(
                nn.Linear(self.hidden_size, self.hidden_size),
                nn.LayerNorm(self.hidden_size),
                nn.ReLU(inplace=True),
                nn.Linear(self.hidden_size, self.hidden_size),
                nn.LayerNorm(self.hidden_size),
                nn.ReLU(inplace=True),
                nn.Linear(self.hidden_size, (self.N_ORDER + 1) * 5)
            )
        elif self.param_out == 'monomial':
            self.N_ORDER = 7
            self.mat_T = self._get_T_matrix_monomial(n_order=self.N_ORDER, n_step=future_steps).to(self.device)
            self.mat_Tp = self._get_Tp_matrix_monomial(n_order=self.N_ORDER, n_step=future_steps).to(self.device)

            self.reg = nn.Sequential(
                nn.Linear(self.hidden_size, self.hidden_size),
                nn.LayerNorm(self.hidden_size),
                nn.ReLU(inplace=True),
                nn.Linear(self.hidden_size, self.hidden_size),
                nn.LayerNorm(self.hidden_size),
                nn.ReLU(inplace=True),
                nn.Linear(self.hidden_size, (self.N_ORDER + 1) * 5)
            )
        elif self.param_out == 'none':
            self.reg = nn.Sequential(
                nn.Linear(self.hidden_size, self.hidden_size),
                nn.LayerNorm(self.hidden_size),
                nn.ReLU(inplace=True),
                nn.Linear(self.hidden_size, self.hidden_size),
                nn.LayerNorm(self.hidden_size),
                nn.ReLU(inplace=True),
                nn.Linear(self.hidden_size, self.future_steps * 5)
            )
        else:
            raise NotImplementedError

    def _get_T_matrix_bezier(self, n_order, n_step):
        ts = np.linspace(0.0, 1.0, n_step, endpoint=True)
        T = []
        for i in range(n_order + 1):
            coeff = math.comb(n_order, i) * (1.0 - ts) ** (n_order - i) * ts ** i
            T.append(coeff)
        return torch.Tensor(np.array(T).T)

    def _get_Tp_matrix_bezier(self, n_order, n_step):
        # ~ 1st derivatives
        ts = np.linspace(0.0, 1.0, n_step, endpoint=True)
        Tp = []
        for i in range(n_order):
            coeff = n_order * math.comb(n_order - 1, i) * (1.0 - ts) ** (n_order - 1 - i) * ts ** i
            Tp.append(coeff)
        return torch.Tensor(np.array(Tp).T)

    def _get_T_matrix_monomial(self, n_order, n_step):
        ts = np.linspace(0.0, 1.0, n_step, endpoint=True)
        T = []
        for i in range(n_order + 1):
            coeff = ts ** i
            T.append(coeff)
        return torch.Tensor(np.array(T).T)

    def _get_Tp_matrix_monomial(self, n_order, n_step):
        # ~ 1st derivatives
        ts = np.linspace(0.0, 1.0, n_step, endpoint=True)
        Tp = []
        for i in range(n_order):
            coeff = (i + 1) * (ts ** i)
            Tp.append(coeff)
        return torch.Tensor(np.array(Tp).T)

    def forward(self,
                ctx: torch.Tensor,
                actors: torch.Tensor,
                actor_idcs: List[Tensor],
                tgt_feat: torch.Tensor,
                tgt_rpes: torch.Tensor):
        res_cls, res_reg, res_aux = [], [], []

        tgt_rpes = self.proj_rpe(tgt_rpes)  # [n_av, 128]
        if len(tgt_feat.shape) == 1:
            tgt_feat = tgt_feat.unsqueeze(0)

        tgt = self.proj_tgt(torch.cat([tgt_feat, tgt_rpes], dim=-1))

        for idx, a_idcs in enumerate(actor_idcs):
            _ctx = ctx[idx].unsqueeze(0)
            _actors = actors[a_idcs]

            cls_embed = self.ctx_proj(_ctx).view(-1, self.num_modes, self.hidden_size).permute(1, 0, 2)
            cls_embed = self.ctx_sat(cls_embed)

            actor_embed = self.actor_proj(_actors).view(-1, self.num_modes, self.hidden_size).permute(1, 0, 2)

            tgt_embed = torch.zeros_like(actor_embed)

            tgt_embed[0] = tgt[idx].unsqueeze(0)

            embed = cls_embed + actor_embed + tgt_embed

            cls = self.cls(cls_embed).view(self.num_modes, -1)

            if self.param_out == 'bezier':
                param = self.reg(embed).view(self.num_modes, -1, self.N_ORDER + 1, 5)
                reg_param = param[..., :2]
                reg_param = reg_param.permute(1, 0, 2, 3)
                reg = torch.matmul(self.mat_T, reg_param)
                vel = torch.matmul(self.mat_Tp, torch.diff(reg_param, dim=2)) / (self.future_steps * 0.1)
                cov_param = param[..., 2:]
                cov_param = cov_param.permute(1, 0, 2, 3)
                cov = torch.matmul(self.mat_T, cov_param)
                cov_vel = torch.matmul(self.mat_Tp, torch.diff(cov_param, dim=2)) / (self.future_steps * 0.1)

            elif self.param_out == 'monomial':
                param = self.reg(embed).view(self.num_modes, -1, self.N_ORDER + 1, 5)
                reg_param = param[..., :2]
                reg_param = reg_param.permute(1, 0, 2, 3)
                reg = torch.matmul(self.mat_T, reg_param)
                vel = torch.matmul(self.mat_Tp, reg_param[:, :, 1:, :]) / (self.future_steps * 0.1)
                cov_param = param[..., 2:]
                cov_param = cov_param.permute(1, 0, 2, 3)
                cov = torch.matmul(self.mat_T, cov_param)
                cov_vel = torch.matmul(self.mat_Tp, torch.diff(cov_param, dim=2)) / (self.future_steps * 0.1)

            elif self.param_out == 'none':
                param = self.reg(embed).view(self.num_modes, -1, self.N_ORDER + 1, 5)
                reg = param[..., :2]
                reg = reg.permute(1, 0, 2, 3)
                vel = torch.gradient(reg, dim=-2)[0] / 0.1
                cov = param[..., 2:]
                cov = cov.permute(1, 0, 2, 3)
                cov_vel = torch.gradient(cov, dim=-2)[0] / 0.1

            reg = torch.cat([reg, torch.exp(cov)], dim=-1)

            cls = cls.permute(1, 0)
            cls = F.softmax(cls * 1.0, dim=1)
            res_cls.append(cls)
            res_reg.append(reg)
            if self.param_out == 'none':
                res_aux.append((vel, cov_vel, None))  # ! None is a placeholder
            else:
                res_aux.append((vel, cov_vel, param))

        return res_cls, res_reg, res_aux


class ScenePredNet(nn.Module):
    # Initialization
    def __init__(self, cfg, device):
        super(ScenePredNet, self).__init__()
        self.device = device

        self.actor_net = ActorNet(n_in=cfg['in_actor'],
                                  hidden_size=cfg['d_actor'],
                                  n_fpn_scale=cfg['n_fpn_scale'])

        self.lane_net = LaneNet(device=self.device,
                                in_size=cfg['in_lane'],
                                hidden_size=cfg['d_lane'],
                                dropout=cfg['dropout'])

        self.fusion_net = FusionNet(device=self.device, config=cfg)

        self.pred_scene = SceneDecoder(device=self.device,
                                       param_out=cfg['param_out'],
                                       hidden_size=cfg['d_embed'],
                                       future_steps=cfg['g_pred_len'],
                                       num_modes=cfg['g_num_modes'])

    def forward(self, data):
        actors, actor_idcs, lanes, lane_idcs, rpe, tgt_nodes, tgt_rpe = data

        # * actors/lanes encoding
        actors = self.actor_net(actors)  # output: [N_{actor}, 128]
        lanes = self.lane_net(lanes)  # output: [N_{lane}, 128]
        # tgt encode
        tgt_feat = self.lane_net(tgt_nodes)  # output: [1, 128]
        # * fusion
        actors, lanes, cls = self.fusion_net(actors, actor_idcs, lanes, lane_idcs, rpe)
        # * decoding
        out = self.pred_scene(cls, actors, actor_idcs, tgt_feat, tgt_rpe)

        return out

    def pre_process(self, data):
        actors = gpu(data['ACTORS'], self.device)
        actor_idcs = gpu(data['ACTOR_IDCS'], self.device)
        lanes = gpu(data['LANES'], self.device)
        lane_idcs = gpu(data['LANE_IDCS'], self.device)
        rpe = gpu(data['RPE'], self.device)
        tgt_nodes = gpu(data['TGT_NODES'], self.device)
        tgt_rpe = gpu(data['TGT_RPE'], self.device)

        return actors, actor_idcs, lanes, lane_idcs, rpe, tgt_nodes, tgt_rpe
