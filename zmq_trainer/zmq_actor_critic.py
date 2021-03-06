from headers import *
import numpy as np
import common
import zmq_trainer.zmq_util
import random
import utils
import time
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.autograd import Variable
from zmq_trainer.zmqsimulator import SimulatorProcess, SimulatorMaster, ensure_proc_terminate


flag_max_lrate = 1e-3
flag_min_lrate = 1e-5
flag_max_kl_diff = 100 #2e-2
flag_min_kl_diff = 0   #1e-4
flag_lrate_coef = 1.5

class ZMQA3CTrainer(AgentTrainer):
    def __init__(self, name, model_creator, obs_shape, act_shape, args):
        super(ZMQA3CTrainer, self).__init__()
        self.name = name
        self.policy = model_creator()
        assert isinstance(self.policy, torch.nn.Module), \
            'ZMQ_A3C_Network must be an instantiated instance of torch.nn.Module'

        self.obs_shape = obs_shape
        self.act_shape = act_shape
        self.act_dim = sum(act_shape)
        # training args
        self.args = args
        self.multi_target = args['multi_target']
        self.mask_feature = args['mask_feature'] if 'mask_feature' in args else False
        self.gamma = args['gamma'] if 'gamma' in args else 0.99
        self.lrate = args['lrate'] if 'lrate' in args else 0.001
        self.batch_size = args['batch_size'] if 'batch_size' in args else 64
        self.grad_batch = args['grad_batch'] if 'grad_batch' in args else 1
        self.accu_grad_steps = 0
        self.accu_ret_dict = dict()
        if 't_max' not in args:
            args['t_max'] = 5
        self.t_max = args['t_max']
        if 'q_loss_coef' in args:
            self.q_loss_coef = args['q_loss_coef']
        else:
            self.q_loss_coef = 1.0
        if ('logits_penalty' in args) and (args['logits_penalty'] is not None):
            self.logit_loss_coef = args['logits_penalty']
            print("[Trainer] Using Logits Loss Coef = %.4f" % self.logit_loss_coef)
        else:
            self.logit_loss_coef = None
        if 'optimizer' not in args:
            self.optim = None
        elif args['optimizer'] == 'adam':
            self.optim = optim.Adam(self.policy.parameters(), lr=self.lrate, weight_decay=args['weight_decay'])  #,betas=(0.5,0.999))
        else:
            self.optim = optim.RMSprop(self.policy.parameters(), lr=self.lrate, weight_decay=args['weight_decay'])
        self.grad_norm_clip = args['grad_clip'] if 'grad_clip' in args else None
        self.adv_norm = args['adv_norm'] if 'adv_norm' in args else False
        self.rew_clip = args['rew_clip'] if 'rew_clip' in args else None
        self._hidden = None
        self._normal_execution = True

    def set_greedy_execution(self):
        self._normal_execution = False

    def _create_feature_tensor(self, feature, return_variable=True, volatile=False):
        # feature: a list of list of numpy.array
        ret = torch.from_numpy(np.array(feature, dtype=np.uint8)).type(ByteTensor).type(FloatTensor)
        if return_variable:
            ret = Variable(ret, volatile=volatile)
        return ret

    def _create_target_tensor(self, targets, return_variable=True, volatile=False):
        batch = len(targets)
        seq_len = len(targets[0])
        target_n = torch.zeros(batch, seq_len, self.policy.n_target_instructions).type(FloatTensor)
        ids = torch.from_numpy(np.array(targets)).type(LongTensor).view(batch, seq_len, 1)
        target_n.scatter_(2, ids, 1.0)
        if return_variable:
            target_n = Variable(target_n, volatile=volatile)
        return target_n

    def _create_gpu_tensor(self, frames, return_variable=True, volatile=False):
        # convert to tensor
        if isinstance(frames, np.ndarray): frames = [[torch.from_numpy(frames).type(ByteTensor)]]
        if not isinstance(frames, list): frames=[[frames]]
        """
        for i in range(len(frames)):
            if not isinstance(frames[i], list): frames[i] = [frames[i]]
            for j in range(len(frames[i])):
                if isinstance(frames[i][j], np.ndarray):
                    frames[i][j] = torch.from_numpy(frames[i][j]).type(ByteTensor)
        """
        tensor = [torch.stack(dat, dim=0) for dat in frames]
        gpu_tensor = torch.stack(tensor, dim=0).permute(0, 1, 4, 2, 3).type(FloatTensor)  # [batch, ....]
        if self.args['segment_input'] != 'index':
            if self.args['depth_input'] or ('attentive' in self.args['model_name']):
                gpu_tensor /= 256.0  # special hack here for depth info
            else:
                gpu_tensor = (gpu_tensor - 128.0) / 128.0
        if return_variable:
            gpu_tensor = Variable(gpu_tensor, volatile=volatile)
        return gpu_tensor

    def _create_gpu_hidden(self, tensor, return_variable=True, volatile=False):
        if not isinstance(tensor, list): tensor = [tensor]
        # convert to gpu tensor
        """
        for i in range(len(tensor)):
            if isinstance(tensor[i], tuple):
                if isinstance(tensor[i][0], np.ndarray):
                    tensor[i] = (torch.from_numpy(tensor[i][0]).type(FloatTensor),
                                 torch.from_numpy(tensor[i][1]).type(FloatTensor))
            else:
                if isinstance(tensor[i], np.ndarray):
                    tensor[i] = torch.from_numpy(tensor[i]).type(FloatTensor)
        """
        if isinstance(tensor[0], tuple):
            g = torch.cat([h[0] for h in tensor], dim=1)
            c = torch.cat([h[1] for h in tensor], dim=1)
            if return_variable:
                g = Variable(g, volatile=volatile)
                c = Variable(c, volatile=volatile)
            return (g, c)
        else:
            h = torch.cat(tensor, dim=1)
            if return_variable:
                h = Variable(h, volatile=volatile)
            return h

    def get_init_hidden(self):
        return self.policy.get_zero_state()

    def reset_agent(self):
        self._hidden = self.get_init_hidden()

    def action(self, obs, hidden=None, return_numpy=False, target=None, temperature=None, mask_input=None):
        if hidden is None:
            hidden = self._hidden
            self._hidden = None
        assert (hidden is not None), '[ZMQA3CTrainer] Currently only support recurrent policy, please input last hidden state!'
        obs = self._create_gpu_tensor(obs, return_variable=True, volatile=True)  # [batch, 1, n, m, channel]
        hidden = self._create_gpu_hidden(hidden, return_variable=True, volatile=True)  # a list of hidden tensors
        if target is not None:
            target = self._create_target_tensor(target, return_variable=True, volatile=True)
        if mask_input is not None:
            mask_input = self._create_feature_tensor(mask_input, return_variable=True, volatile=True)
        act, nxt_hidden = self.policy(obs, hidden, return_value=False, sample_action=self._normal_execution,
                                      unpack_hidden=True, return_tensor=True, target=target,
                                      temperature=temperature, extra_input_feature=mask_input)
        if self._hidden is None:
            self._hidden = nxt_hidden
        if return_numpy: # currently only for action
            act = act.cpu().numpy()
        return act, nxt_hidden   # NOTE: everything remains on gpu!

    def train(self):
        self.policy.train()

    def eval(self):
        self.policy.eval()

    def process_experience(self, idx, act, rew, done, terminal, info):
        pass

    def update(self, obs, init_hidden, act, rew, done,
                target=None, supervision_mask=None, mask_input=None,
                return_kl_divergence=True):
        """
        :param obs:  list of list of [dims]...
        :param init_hidden: list of [layer, 1, units]
        :param act: [batch, seq_len]
        :param rew: [batch, seq_len]
        :param done: [batch, seq_len]
        :param target: [batch, seq_len, n_instruction] or None (when single-target)
        :param supervision_mask: timesteps marked with supervised learning loss [batch, seq_len] or None (pure RL)
        """
        tt = time.time()

        # reward clipping
        if self.rew_clip is not None: rew = np.clip(rew, -self.rew_clip, self.rew_clip)

        # convert data to Variables
        obs = self._create_gpu_tensor(obs, return_variable=True)  # [batch, t_max+1, dims...]
        init_hidden = self._create_gpu_hidden(init_hidden, return_variable=True)  # [layers, batch, units]
        if target is not None:
            target = self._create_target_tensor(target, return_variable=True)
        if mask_input is not None:
            mask_input = self._create_feature_tensor(mask_input, return_variable=True)
        act = Variable(torch.from_numpy(act).type(LongTensor))  # [batch, t_max]
        mask = 1.0 - torch.from_numpy(done).type(FloatTensor) # [batch, t_max]
        mask_var = Variable(mask)
        sup_mask = None if supervision_mask is None else torch.from_numpy(supervision_mask).type(ByteTensor)  # [batch, t_max]

        time_counter[0] += time.time() - tt

        batch_size = self.batch_size
        t_max = self.t_max
        gamma = self.gamma

        tt = time.time()

        if self.accu_grad_steps == 0:  # clear grad
            self.optim.zero_grad()

        # forward pass
        logits = []
        logprobs = []
        values = []
        t_obs_slices = torch.chunk(obs, t_max + 1, dim=1)
        obs_slices = [t.contiguous() for t in t_obs_slices]
        if target is not None:
            t_target_slices = torch.chunk(target, t_max + 1, dim=1)
            target_slices = [t.contiguous() for t in t_target_slices]
        if mask_input is not None:
            t_mask_input_slices = torch.chunk(mask_input, t_max + 1, dim=1)
            mask_input_slices = [m.contiguous() for m in t_mask_input_slices]
        cur_h = init_hidden
        for t in range(t_max):
            #cur_obs = obs[:, t:t+1, ...].contiguous()
            cur_obs = obs_slices[t]
            t_target = None if target is None else target_slices[t]
            t_mask = None if mask_input is None else mask_input_slices[t]
            cur_logp, cur_val, nxt_h = self.policy(cur_obs, cur_h,
                                                   target=t_target,
                                                   extra_input_feature=t_mask)
            cur_h = self.policy.mark_hidden_states(nxt_h, mask_var[:, t:t+1])
            values.append(cur_val)
            logprobs.append(cur_logp)
            logits.append(self.policy.logits)
        #cur_obs = obs[:, t_max:t_max + 1, ...].contiguous()
        cur_obs = obs_slices[-1]
        t_target = None if target is None else target_slices[-1]
        t_mask = None if mask_input is None else mask_input_slices[-1]
        nxt_val = self.policy(cur_obs, cur_h,
                              only_value=True, return_tensor=True,
                              target=t_target, extra_input_feature=t_mask)
        V = torch.cat(values, dim=1)  # [batch, t_max]
        P = torch.cat(logprobs, dim=1)  # [batch, t_max, n_act]
        L = torch.cat(logits, dim=1)
        p_ent = torch.mean(self.policy.entropy(L))  # compute entropy
        #L_norm = torch.mean(torch.norm(L, dim=-1))
        L_norm = torch.mean(torch.sum(L * L, dim=-1))   # L^2 penalty

        # estimate accumulative rewards
        rew = torch.from_numpy(rew).type(FloatTensor)  # [batch, t_max]
        R = []
        cur_R = nxt_val.squeeze()  # [batch]
        for t in range(t_max-1, -1, -1):
            cur_mask = mask[:, t]
            cur_R = rew[:, t] + gamma * cur_R * cur_mask
            R.append(cur_R)
        R.reverse()
        R = Variable(torch.stack(R, dim=1))  # [batch, t_max]

        # estimate advantage
        A_dat = R.data - V.data  # stop gradient here
        std_val = None
        if self.adv_norm:   # perform advantage normalization
            std_val = max(A_dat.std(), 0.1)
            A_dat = (A_dat - A_dat.mean()) / (std_val + 1e-10)
        if sup_mask is not None:  # supervision
            A_dat[sup_mask > 0] = 1.0    # change A * log P(a) to log P(supervised_a), act has been modified in zmq_util
        A = Variable(A_dat)
        # [optional]  A = Variable(rew) - V

        # compute loss
        #critic_loss = F.smooth_l1_loss(V, R)
        critic_loss = torch.mean((R - V) ** 2)
        pg_loss = -torch.mean(self.policy.logprob(act, P) * A)
        if self.args['entropy_penalty'] is not None:
            pg_loss -= self.args['entropy_penalty'] * p_ent  # encourage exploration
        loss = self.q_loss_coef * critic_loss + pg_loss
        if self.logit_loss_coef is not None:
            loss += self.logit_loss_coef * L_norm

        # backprop
        if self.grad_batch > 1:
            loss = loss / float(self.grad_batch)
        loss.backward()

        ret_dict = dict(pg_loss=pg_loss.data.cpu().numpy()[0],
                        policy_entropy=p_ent.data.cpu().numpy()[0],
                        critic_loss=critic_loss.data.cpu().numpy()[0],
                        logits_norm=L_norm.data.cpu().numpy()[0])
        if std_val is not None:
            ret_dict['adv_norm'] = std_val

        if self.accu_grad_steps == 0:
            self.accu_ret_dict = ret_dict
        else:
            for k in ret_dict:
                self.accu_ret_dict[k] += ret_dict[k]

        self.accu_grad_steps += 1
        if self.accu_grad_steps < self.grad_batch:  # do not update parameter now
            time_counter[1] += time.time() - tt
            return None

        # update parameters
        for k in self.accu_ret_dict:
            self.accu_ret_dict[k] /= self.grad_batch
        ret_dict = self.accu_ret_dict
        self.accu_grad_steps = 0

        # grad clip
        if self.grad_norm_clip is not None:
            utils.clip_grad_norm(self.policy.parameters(), self.grad_norm_clip)
        self.optim.step()

        if return_kl_divergence:
            cur_h = init_hidden
            new_logprobs = []
            for t in range(t_max):
                # cur_obs = obs[:, t:t+1, ...].contiguous()
                cur_obs = obs_slices[t]
                t_target = target_slices[t] if self.multi_target else None
                t_mask = None if mask_input is None else mask_input_slices[t]
                cur_logp, nxt_h = self.policy(cur_obs, cur_h, return_value=False,
                                              target=t_target, extra_input_feature=t_mask)
                cur_h = self.policy.mark_hidden_states(nxt_h, mask_var[:, t:t + 1])
                new_logprobs.append(cur_logp)
            new_P = torch.cat(new_logprobs, dim=1)
            kl = self.policy.kl_divergence(new_P, P).mean().data.cpu()[0]
            ret_dict['KL(P_new||P_old)'] = kl

            if kl > flag_max_kl_diff:
                self.lrate /= flag_lrate_coef
                self.optim.__dict__['param_groups'][0]['lr']=self.lrate
                ret_dict['!!![NOTE]:'] = ('------>>>> KL is too large (%.6f), decrease lrate to %.5f' % (kl, self.lrate))
            elif (kl < flag_min_kl_diff) and (self.lrate < flag_max_lrate):
                self.lrate *= flag_lrate_coef
                self.optim.__dict__['param_groups'][0]['lr'] = self.lrate
                ret_dict['!!![NOTE]:'] = ('------>>>> KL is too small (%.6f), increase lrate to %.5f' % (kl, self.lrate))


        time_counter[1] += time.time() - tt
        return ret_dict

    def is_rnn(self):
        return True
