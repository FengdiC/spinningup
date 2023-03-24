import random

import matplotlib.pyplot as plt
import torch
import numpy as np
import os
import sys
import inspect

currentdir = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
parentdir = os.path.dirname(currentdir)
sys.path.insert(0, parentdir)
import gym
from gym import spaces
from Components import logger
from dm_control import suite

from torch.optim import Adam
import time
import spinup.algos.pytorch.ppo.core as core
from spinup.utils.logx import EpochLogger
from spinup.utils.mpi_pytorch import setup_pytorch_for_mpi, sync_params, mpi_avg_grads
from spinup.utils.mpi_tools import mpi_fork, mpi_avg, proc_id, mpi_statistics_scalar, num_procs

def set_one_thread():
    os.environ['OMP_NUM_THREADS'] = '1'
    os.environ['MKL_NUM_THREADS'] = '1'
    torch.set_num_threads(1)

def random_search(seed):
    rng = np.random.RandomState(seed=seed)

    # # choice 1
    # pi_lr = rng.choice([3,9,20,30,40,50])/10000.0
    # choice 2
    pi_lr = rng.choice([3,10, 30]) / 10000.0
    gamma_coef = rng.randint(low=50, high=500)/100.0
    scale = rng.randint(low=1, high=150)
    target_kl = rng.randint(low=0.01*100, high=0.3*100)/100.0
    vf_lr = rng.randint(low=3, high=50)/10000.0
    gamma = rng.choice([0.95,0.97,0.99,0.995])
    hid = np.array([[16,16],[32,32],[64,64]])
    critic_hid = rng.choice(range(hid.shape[0]))
    critic_hid = hid[critic_hid]

    hyperparameters = {"pi_lr":pi_lr,"gamma_coef":gamma_coef, "scale":scale, "target_kl":target_kl,
                       "vf_lr":vf_lr,"critic_hid":list(critic_hid),"gamma":gamma}

    return hyperparameters

def argsparser():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--log_dir', type=str, default='./')
    parser.add_argument('--env', type=str, default="cartpole")
    parser.add_argument('--type', type=str, default="swingup")
    parser.add_argument('--hid', type=list, default=[64,32])
    parser.add_argument('--critic_hid', type=list, default=[128, 128])
    parser.add_argument('--l', type=int, default=2)
    parser.add_argument('--gamma', type=float, default=0.99)
    parser.add_argument('--seed', '-s', type=int, default=0)
    parser.add_argument('--cpu', type=int, default=4)
    parser.add_argument('--steps', type=int, default=4000)
    parser.add_argument('--epochs', type=int, default=50)
    parser.add_argument('--naive', type=bool, default=False)
    parser.add_argument('--exp_name', type=str, default='ppo')
    parser.add_argument('--clip_ratio', type=float, default=0.2)
    parser.add_argument('--pi_lr', type=float, default=3e-4)

    parser.add_argument('--scale', type=float, default=1.0)
    parser.add_argument('--gamma_coef',  type=float, default=1.0)
    parser.add_argument('--vf_lr', type=float, default=1e-3)
    args = parser.parse_args()
    return args

class PPOBuffer:
    """
    A buffer for storing trajectories experienced by a PPO agent interacting
    with the environment, and using Generalized Advantage Estimation (GAE-Lambda)
    for calculating the advantages of state-action pairs.
    """

    def __init__(self, obs_dim, act_dim, size, gamma=0.99, lam=0.95):
        self.obs_buf = np.zeros(core.combined_shape(size, obs_dim), dtype=np.float32)
        self.act_buf = np.zeros(core.combined_shape(size, act_dim), dtype=np.float32)
        self.adv_buf = np.zeros(size, dtype=np.float32)
        self.rew_buf = np.zeros(size, dtype=np.float32)
        self.tim_buf = np.zeros(size, dtype=np.int32)
        self.ret_buf = np.zeros(size, dtype=np.float32)
        self.val_buf = np.zeros(size, dtype=np.float32)
        self.logp_buf = np.zeros(size, dtype=np.float32)
        self.gamma, self.lam = gamma, lam
        self.ptr, self.path_start_idx, self.max_size = 0, 0, size

    def store(self, obs, act, rew, tim, val, logp):
        """
        Append one timestep of agent-environment interaction to the buffer.
        """
        assert self.ptr < self.max_size  # buffer has to have room so you can store
        self.obs_buf[self.ptr] = obs
        self.act_buf[self.ptr] = act
        self.rew_buf[self.ptr] = rew
        self.tim_buf[self.ptr] = tim
        self.val_buf[self.ptr] = val
        self.logp_buf[self.ptr] = logp
        self.ptr += 1

    def finish_path(self, last_val=0):
        """
        Call this at the end of a trajectory, or when one gets cut off
        by an epoch ending. This looks back in the buffer to where the
        trajectory started, and uses rewards and value estimates from
        the whole trajectory to compute advantage estimates with GAE-Lambda,
        as well as compute the rewards-to-go for each state, to use as
        the targets for the value function.

        The "last_val" argument should be 0 if the trajectory ended
        because the agent reached a terminal state (died), and otherwise
        should be V(s_T), the value function estimated for the last state.
        This allows us to bootstrap the reward-to-go calculation to account
        for timesteps beyond the arbitrary episode horizon (or epoch cutoff).
        """

        path_slice = slice(self.path_start_idx, self.ptr)
        rews = np.append(self.rew_buf[path_slice], last_val)
        vals = np.append(self.val_buf[path_slice], last_val)

        # the next two lines implement GAE-Lambda advantage calculation
        deltas = rews[:-1] + self.gamma * vals[1:] - vals[:-1]
        self.adv_buf[path_slice] = core.discount_cumsum(deltas, self.gamma * self.lam)

        # the next line computes rewards-to-go, to be targets for the value function
        self.ret_buf[path_slice] = core.discount_cumsum(rews, self.gamma)[:-1]

        self.path_start_idx = self.ptr

    def get(self):
        """
        Call this at the end of an epoch to get all of the data from
        the buffer, with advantages appropriately normalized (shifted to have
        mean zero and std one). Also, resets some pointers in the buffer.
        """
        assert self.ptr == self.max_size  # buffer has to be full before you can get
        self.ptr, self.path_start_idx = 0, 0
        # the next two lines implement the advantage normalization trick
        adv_mean, adv_std = mpi_statistics_scalar(self.adv_buf)
        self.adv_buf = (self.adv_buf - adv_mean) / (adv_std + 0.001)
        data = dict(obs=self.obs_buf, act=self.act_buf, ret=self.ret_buf, tim=self.tim_buf,
                    adv=self.adv_buf, logp=self.logp_buf)
        return {k: torch.as_tensor(v, dtype=torch.float32) for k, v in data.items()}

def compute_correction(env,agent,gamma,policy=np.array([None,None])):
    # get the policy
    states = env.get_states()
    if (policy==None).all():
        policy = agent.pi.logits_net(torch.as_tensor(states, dtype=torch.float32))
        policy=torch.nn.functional.softmax(policy).detach().numpy()
    # policy = np.ones((25,8))/8.0

    # get transition matrix P
    P = env.transition_matrix(policy)
    n = env.num_pt
    # # check if the matrix is a transition matrix
    # print(np.sum(P,axis=1))
    power = 1
    err = np.matmul(np.ones(n**2),np.linalg.matrix_power(P,power+1))-\
          np.matmul(np.ones(n**2), np.linalg.matrix_power(P, power))
    err = np.sum(np.abs(err))
    while err > 1.2 and power<10:
        power+=1
        err = np.matmul(np.ones(n**2), np.linalg.matrix_power(P,  power + 1)) - \
              np.matmul(np.ones(n**2), np.linalg.matrix_power(P, power))
        err = np.sum(np.abs(err))
    # print(np.sum(np.linalg.matrix_power(P, 3),axis=1))
    d_pi = np.matmul(np.ones(n**2)/float(n**2), np.linalg.matrix_power(P, power + 1))
    # print("stationary distribution",d_pi,np.sum(d_pi))

    if np.sum(d_pi - np.matmul(np.transpose(d_pi),P))>0.001:
        print("not the stationary distribution")

    # compute the special transition function M
    M = np.matmul(np.diag(d_pi) , P)
    M = np.matmul(M, np.diag(1/d_pi))

    correction = np.matmul(np.linalg.inv(np.eye(n**2)-gamma* np.transpose(M)) , (1-gamma) * 1/(d_pi*n**2))
    discounted = correction * d_pi
    return correction,d_pi,discounted

def weighted_ppo(env_name, actor_critic=core.MLPWeightedActorCritic, ac_kwargs=dict(), seed=0,
                 steps_per_epoch=4000, epochs=50, gamma=0.99, clip_ratio=0.2, pi_lr=3e-4,
                 vf_lr=1e-3, train_pi_iters=80, train_v_iters=80, lam=0.97, max_ep_len=1000,
                 target_kl=0.01, logger_kwargs=dict(), save_freq=10, scale=1.0, gamma_coef=1.0):
    """
    Proximal Policy Optimization (by clipping),

    with early stopping based on approximate KL

    Args:
        env_fn : A function which creates a copy of the environment.
            The environment must satisfy the OpenAI Gym API.

        actor_critic: The constructor method for a PyTorch Module with a
            ``step`` method, an ``act`` method, a ``pi`` module, and a ``v``
            module. The ``step`` method should accept a batch of observations
            and return:

            ===========  ================  ======================================
            Symbol       Shape             Description
            ===========  ================  ======================================
            ``a``        (batch, act_dim)  | Numpy array of actions for each
                                           | observation.
            ``v``        (batch,)          | Numpy array of value estimates
                                           | for the provided observations.
            ``logp_a``   (batch,)          | Numpy array of log probs for the
                                           | actions in ``a``.
            ===========  ================  ======================================

            The ``act`` method behaves the same as ``step`` but only returns ``a``.

            The ``pi`` module's forward call should accept a batch of
            observations and optionally a batch of actions, and return:

            ===========  ================  ======================================
            Symbol       Shape             Description
            ===========  ================  ======================================
            ``pi``       N/A               | Torch Distribution object, containing
                                           | a batch of distributions describing
                                           | the policy for the provided observations.
            ``logp_a``   (batch,)          | Optional (only returned if batch of
                                           | actions is given). Tensor containing
                                           | the log probability, according to
                                           | the policy, of the provided actions.
                                           | If actions not given, will contain
                                           | ``None``.
            ===========  ================  ======================================

            The ``v`` module's forward call should accept a batch of observations
            and return:

            ===========  ================  ======================================
            Symbol       Shape             Description
            ===========  ================  ======================================
            ``v``        (batch,)          | Tensor containing the value estimates
                                           | for the provided observations. (Critical:
                                           | make sure to flatten this!)
            ===========  ================  ======================================


        ac_kwargs (dict): Any kwargs appropriate for the ActorCritic object
            you provided to PPO.

        seed (int): Seed for random number generators.

        steps_per_epoch (int): Number of steps of interaction (state-action pairs)
            for the agent and the environment in each epoch.

        epochs (int): Number of epochs of interaction (equivalent to
            number of policy updates) to perform.

        gamma (float): Discount factor. (Always between 0 and 1.)

        clip_ratio (float): Hyperparameter for clipping in the policy objective.
            Roughly: how far can the new policy go from the old policy while
            still profiting (improving the objective function)? The new policy
            can still go farther than the clip_ratio says, but it doesn't help
            on the objective anymore. (Usually small, 0.1 to 0.3.) Typically
            denoted by :math:`\epsilon`.

        pi_lr (float): Learning rate for policy optimizer.

        vf_lr (float): Learning rate for value function optimizer.

        train_pi_iters (int): Maximum number of gradient descent steps to take
            on policy loss per epoch. (Early stopping may cause optimizer
            to take fewer than this.)

        train_v_iters (int): Number of gradient descent steps to take on
            value function per epoch.

        lam (float): Lambda for GAE-Lambda. (Always between 0 and 1,
            close to 1.)

        max_ep_len (int): Maximum length of trajectory / episode / rollout.

        target_kl (float): Roughly what KL divergence we think is appropriate
            between new and old policies after an update. This will get used
            for early stopping. (Usually small, 0.01 or 0.05.)

        logger_kwargs (dict): Keyword args for EpochLogger.

        save_freq (int): How often (in terms of gap between epochs) to save
            the current policy and value function.

    """

    # Special function to avoid certain slowdowns from PyTorch + MPI combo.
    setup_pytorch_for_mpi()

    # Set up logger and save configuration
    logger = EpochLogger(**logger_kwargs)
    logger.save_config(locals())

    # Random seed
    torch.manual_seed(seed)
    np.random.seed(seed)

    # Instantiate environment
    env = suite.load(env_name[0],env_name[1])
    time_step = env.reset()
    obs_dim = np.concatenate([time_step.observation[key] for key in time_step.observation.keys()]).shape[0]
    act_dim = env.action_spec().shape[0]
    observation_space = spaces.Box(low=-1000, high=1000, shape=(obs_dim,))
    action_space = spaces.Box(low=-1, high=1, shape=(act_dim,))

    # Create actor-critic module
    ac = actor_critic(observation_space, action_space, **ac_kwargs)

    # Sync params across processes
    sync_params(ac)

    # Count variables
    var_counts = tuple(core.count_vars(module) for module in [ac.pi, ac.v])
    logger.log('\nNumber of parameters: \t pi: %d, \t v: %d\n' % var_counts)

    # Set up experience buffer
    local_steps_per_epoch = int(steps_per_epoch / num_procs())
    buf = PPOBuffer(obs_dim, act_dim, local_steps_per_epoch, gamma, lam)

    def compute_c_D(data,num_traj=12):
        states = env.get_states()
        states = states.tolist()
        states = [[round(key, 2) for key in item] for item in states]
        count = np.zeros(len(states))
        weighting = np.zeros(data['obs'].size(dim=0))
        numerator = [[] for s in range(len(states))]
        tim = data['tim']
        for i in range(data['obs'].size(dim=0)):
            s = data['obs'][i].numpy().tolist()
            s = [round(key, 2) for key in s]
            idx = states.index(s)
            numerator[idx].append(gamma ** tim[i])
            count[idx] += 1
        numerator = [sum(s) / num_traj for s in numerator]
        numerator = np.array(numerator)
        c_D = data['obs'].size(dim=0) * (1 - gamma) * numerator / (count + 0.001)

        for i in range(data['obs'].size(dim=0)):
            s = data['obs'][i].numpy().tolist()
            s = [round(key, 2) for key in s]
            idx = states.index(s)
            weighting[i] = c_D[idx]
        return weighting

    # Set up function for computing PPO policy loss
    def compute_loss_pi(data):
        obs, act, tim, adv, logp_old = data['obs'], data['act'], data['tim'], \
                                       data['adv'], data['logp']

        # Policy loss
        pi, logp = ac.pi(obs, act)
        _, w = ac.v(obs)
        # weighting = torch.as_tensor(compute_c_D(data), dtype=torch.float32)
        ratio = torch.exp(logp - logp_old)
        clip_adv = torch.clamp(ratio, 1 - clip_ratio, 1 + clip_ratio) * adv
        # loss_pi = -(torch.min(ratio * adv, clip_adv) * weighting).mean()
        loss_pi = -(torch.min(ratio * adv, clip_adv) * (w / scale)).mean()
        emphasis = torch.where(ratio * adv<= clip_adv,torch.ones(tim.size(dim=0)),torch.zeros(tim.size(dim=0)))

        # Useful extra info
        approx_kl = (logp_old - logp).mean().item()
        ent = pi.entropy().mean().item()
        clipped = ratio.gt(1 + clip_ratio) | ratio.lt(1 - clip_ratio)
        clipfrac = torch.as_tensor(clipped, dtype=torch.float32).mean().item()
        pi_info = dict(kl=approx_kl, ent=ent, cf=clipfrac)

        return loss_pi, pi_info, emphasis.numpy()

    # Set up function for computing value loss
    def compute_loss_v(data):
        obs, ret, tim = data['obs'], data['ret'], data['tim']
        v, w = ac.v(obs)
        loss = ((v - ret) ** 2).mean() + gamma_coef * ((w - gamma ** tim * scale) ** 2).mean()
        return loss

    # Set up optimizers for policy and value function
    pi_optimizer = Adam(ac.pi.parameters(), lr=pi_lr)
    vf_optimizer = Adam(ac.v.parameters(), lr=vf_lr)

    # Set up model saving
    logger.setup_pytorch_saver(ac)

    def update():
        data = buf.get()
        # compute the discounted distribution of the old policy
        # correction, d_pi, discounted = compute_correction(env, ac, gamma)

        pi_l_old, pi_info_old, clipped = compute_loss_pi(data)
        pi_l_old = pi_l_old.item()
        v_l_old = compute_loss_v(data).item()

        # # Train policy with multiple steps of gradient descent
        # corrections = []
        # d_pis = []
        # discounteds = []
        # clips=[]
        # clips.append(np.ones(data['tim'].size(dim=0)))
        for i in range(train_pi_iters):
            # # compute the discounted distribution of the old policy
            # correction, d_pi, discounted = compute_correction(env, ac, gamma)
            # corrections.append(correction)
            # d_pis.append(d_pi)
            # discounteds.append(discounted)
            pi_optimizer.zero_grad()
            loss_pi, pi_info, clipped = compute_loss_pi(data)
            # clips.append(clipped)
            kl = mpi_avg(pi_info['kl'])
            if kl > 1.5 * target_kl:
                logger.log('Early stopping at step %d due to reaching max kl.' % i)
                break
            loss_pi.backward()
            mpi_avg_grads(ac.pi)  # average grads across MPI processes
            pi_optimizer.step()

        logger.store(StopIter=i)

        # Value function learning
        for i in range(train_v_iters):
            vf_optimizer.zero_grad()
            loss_v = compute_loss_v(data)
            loss_v.backward()
            mpi_avg_grads(ac.v)  # average grads across MPI processes
            vf_optimizer.step()

        # Log changes from update
        kl, ent, cf = pi_info['kl'], pi_info_old['ent'], pi_info['cf']
        logger.store(LossPi=pi_l_old, LossV=v_l_old,
                     KL=kl, Entropy=ent, ClipFrac=cf,
                     DeltaLossPi=(loss_pi.item() - pi_l_old),
                     DeltaLossV=(loss_v.item() - v_l_old))

    # Prepare for interaction with environment
    start_time = time.time()
    time_step, ep_ret, ep_len = env.reset(), 0, 0
    o = np.concatenate([time_step.observation[key] for key in time_step.observation.keys()])
    rets = []
    avgrets = []

    # Main loop: collect experience in env and update/log each epoch
    for epoch in range(epochs):
        for t in range(local_steps_per_epoch):
            a, v, w, logp = ac.step(torch.as_tensor(o, dtype=torch.float32))

            time_step = env.step(a)
            next_o=np.concatenate([time_step.observation[key] for key in time_step.observation.keys()])
            r = time_step.reward
            d = False
            ep_ret += r
            ep_len += 1

            # save and log
            buf.store(o, a, r, ep_len - 1, v, logp)
            logger.store(VVals=v)

            # Update obs (critical!)
            o = next_o

            timeout = ep_len == max_ep_len
            terminal = d or timeout
            epoch_ended = t == local_steps_per_epoch - 1

            if terminal or epoch_ended:
                if epoch_ended and not (terminal):
                    print('Warning: trajectory cut off by epoch at %d steps.' % ep_len, flush=True)
                # if trajectory didn't reach terminal state, bootstrap value target
                if timeout or epoch_ended:
                    _, v, _, _ = ac.step(torch.as_tensor(o, dtype=torch.float32))
                else:
                    v = 0
                buf.finish_path(v)
                if terminal:
                    # only save EpRet / EpLen if trajectory finished
                    logger.store(EpRet=ep_ret, EpLen=ep_len)
                    rets.append(ep_ret)
                time_step, ep_ret, ep_len = env.reset(), 0, 0
                o = np.concatenate([time_step.observation[key] for key in time_step.observation.keys()])

        # Save model
        if (epoch % save_freq == 0) or (epoch == epochs - 1):
            logger.save_state({'env': env}, None)

        # Perform PPO update!
        update()

        # Log info about epoch
        avgrets.append(np.mean(rets))
        rets = []
        logger.log_tabular('Epoch', epoch)
        logger.log_tabular('EpRet', with_min_and_max=True)
        logger.log_tabular('EpLen', average_only=True)
        logger.log_tabular('VVals', with_min_and_max=True)
        logger.log_tabular('TotalEnvInteracts', (epoch + 1) * steps_per_epoch)
        logger.log_tabular('LossPi', average_only=True)
        logger.log_tabular('LossV', average_only=True)
        logger.log_tabular('DeltaLossPi', average_only=True)
        logger.log_tabular('DeltaLossV', average_only=True)
        logger.log_tabular('Entropy', average_only=True)
        logger.log_tabular('KL', average_only=True)
        logger.log_tabular('ClipFrac', average_only=True)
        logger.log_tabular('StopIter', average_only=True)
        logger.log_tabular('Time', time.time() - start_time)
        logger.dump_tabular()
    return avgrets

def tune_Reacher():
    args = argsparser()
    seeds = range(10)

    # Torch Shenanigans fix
    set_one_thread()

    logger.configure(args.log_dir, ['csv'], log_suffix='weighted-ppo-tune-' + str(args.seed)+ str(args.env))

    returns = []
    for seed in seeds:
        hyperparam = random_search(args.seed)
        checkpoint = 4000
        result = weighted_ppo([args.env,args.type], actor_critic=core.MLPWeightedActorCritic,
                              ac_kwargs=dict(hidden_sizes=args.hid, critic_hidden_sizes=hyperparam['critic_hid']),
                              epochs=args.epochs,
                              gamma=hyperparam['gamma'], target_kl=hyperparam['target_kl'], vf_lr=hyperparam['vf_lr'],
                              pi_lr=hyperparam["pi_lr"],
                              seed=seed, scale=hyperparam['scale'], gamma_coef=hyperparam['gamma_coef'])

        ret = np.array(result)
        print(ret.shape)
        returns.append(ret)
        name = list(hyperparam.values())
        name = [str(s) for s in name]
        name.append(str(seed))
        print("hyperparam", '-'.join(name))
        logger.logkv("hyperparam", '-'.join(name))
        for n in range(ret.shape[0]):
            logger.logkv(str((n + 1) * checkpoint), ret[n])
        logger.dumpkvs()

tune_Reacher()