import torch
import copy
import sys
import logging
import argparse
import configparser
import os
import shutil
import gym
from dynav.utils.navigator import Navigator
from dynav.utils.trainer import Trainer
from dynav.utils.memory import ReplayMemory
from dynav.utils.explorer import Explorer
from dynav.policy.policy_factory import policy_factory


def main():
    parser = argparse.ArgumentParser('Parse configuration file')
    parser.add_argument('--env_config', type=str, default='configs/env.config')
    parser.add_argument('--policy', type=str, default='value_network')
    parser.add_argument('--policy_config', type=str, default='configs/policy.config')
    parser.add_argument('--train_config', type=str, default='configs/train.config')
    parser.add_argument('--output_dir', type=str, default='output')
    parser.add_argument('--weights', type=str)
    parser.add_argument('--gpu', default=False, action='store_true')
    args = parser.parse_args()

    env_config = configparser.RawConfigParser()
    env_config.read(args.env_config)

    # configure paths
    output_dir = os.path.join('data', args.output_dir)
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
        os.mkdir(output_dir)
    else:
        os.mkdir(output_dir)
    log_file = os.path.join(output_dir, 'output.log')
    shutil.copy(args.train_config, output_dir)
    weight_file = os.path.join(output_dir, 'trained_model.pth')

    # configure logging
    file_handler = logging.FileHandler(log_file, mode='w')
    stdout_handler = logging.StreamHandler(sys.stdout)
    logging.basicConfig(level=logging.DEBUG, handlers=[stdout_handler, file_handler],
                        format='%(asctime)s, %(levelname)s: %(message)s', datefmt="%Y-%m-%d %H:%M:%S")

    # configure policy
    policy = policy_factory[args.policy]()
    if not policy.trainable:
        parser.error('Policy has to be trainable')
    if args.policy_config is None:
        parser.error('Policy config has to be specified for a trainable network')
    policy_config = configparser.RawConfigParser()
    policy_config.read(args.policy_config)
    policy.configure(policy_config)

    # configure environment
    env = gym.make('CrowdSim-v0')
    env.configure(env_config)
    navigator = Navigator(env_config, 'navigator')
    env.set_navigator(navigator)
    if args.policy == 'value_network':
        policy.set_env(env)

    # read training parameters
    if args.train_config is None:
        parser.error('Train config has to be specified for a trainable network')
    train_config = configparser.RawConfigParser()
    train_config.read(args.train_config)
    train_epochs = train_config.getint('train', 'train_epochs')
    train_episodes = train_config.getint('train', 'train_episodes')
    sample_episodes = train_config.getint('train', 'sample_episodes')
    test_interval = train_config.getint('train', 'test_interval')
    val_episodes = train_config.getint('train', 'val_episodes')
    capacity = train_config.getint('train', 'capacity')
    epsilon_start = train_config.getfloat('train', 'epsilon_start')
    epsilon_end = train_config.getfloat('train', 'epsilon_end')
    epsilon_decay = train_config.getfloat('train', 'epsilon_decay')
    checkpoint_interval = train_config.getint('train', 'checkpoint_interval')

    # configure trainer and explorer
    memory = ReplayMemory(capacity)
    model = policy.get_model()
    device = torch.device("cuda:0" if torch.cuda.is_available() and args.gpu else "cpu")
    logging.info('Using device: {}'.format(device))
    trainer = Trainer(train_config, model, memory, device)
    navigator.policy.set_device(device)
    gamma = navigator.policy.gamma
    explorer = Explorer(env, navigator, device, memory, gamma)

    # imitation learning
    il_episodes = train_config.getint('imitation_learning', 'il_episodes')
    il_policy = train_config.get('imitation_learning', 'il_policy')
    il_epochs = train_config.getint('imitation_learning', 'il_epochs')
    il_policy = policy_factory[il_policy]()
    navigator.set_policy(il_policy)
    explorer.run_k_episodes(il_episodes, 'train', update_memory=True, imitation_learning=True)
    trainer.optimize_batch(il_epochs)
    explorer.update_stabilized_model(model)

    # reinforcement learning
    navigator.set_policy(policy)
    episode = 0
    while episode < train_episodes:
        # epsilon-greedy
        if episode < epsilon_decay:
            epsilon = epsilon_start + (epsilon_end - epsilon_start) / epsilon_decay * episode
        else:
            epsilon = epsilon_end
        navigator.policy.set_epsilon(epsilon)

        # test
        if episode % test_interval == 0:
            explorer.run_k_episodes(val_episodes, 'val', episode=episode)
            explorer.run_k_episodes(env.test_cases, 'test', episode=episode)
            explorer.update_stabilized_model(model)

        # sample k episodes into memory and optimize over the generated memory
        explorer.run_k_episodes(sample_episodes, 'train', update_memory=True, episode=episode)
        trainer.optimize_batch(train_epochs)
        episode += 1

        if episode != 0 and episode % checkpoint_interval == 0:
            torch.save(model.state_dict(), weight_file)


if __name__ == '__main__':
    main()
