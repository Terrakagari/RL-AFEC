from __future__ import print_function

import numpy as np
from tqdm import tqdm
import multiprocessing as mp
import math
import datetime
from absl import app
from absl import flags

from env import Environment
from game import Game
from model_mine import Network
from config import get_config
import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = "2"


FLAGS = flags.FLAGS
flags.DEFINE_integer('num_agents', 5, 'number of agents')
flags.DEFINE_string('baseline', 'avg', 'avg: use average reward as baseline, best: best reward as baseline')
flags.DEFINE_integer('num_iter', 100, 'Number of iterations each agent would run')

GRADIENTS_CHECK=True

def central_agent(config, game, model_weights_queues, experience_queues):
    network = Network(config, game.input_state_dims, game.action_dim, master=True)
    network.save_hyperparams(config)
    start_step = network.restore_ckpt()
    for step in tqdm(range(start_step, config.max_step), ncols=70, initial=start_step):
        network.ckpt.step.assign_add(1)
        if config.model == 'actor_critic':
            model_weights = network.model.get_weights()
        elif config.model == 'pure_policy':
            model_weights = network.policy_model.get_weights()

        # print(config.num_agents)
        for i in range(config.num_agents):
            model_weights_queues[i].put(model_weights)

        if config.model == 'actor_critic':
            #assemble experiences from the agents
            s_batch = []
            a_batch = []
            r_batch = []

            for i in range(config.num_agents):
                s_batch_agent, a_batch_agent, r_batch_agent = experience_queues[i].get()
              
                assert len(s_batch_agent) == config.num_iter, \
                    (len(s_batch_agent), len(a_batch_agent), len(r_batch_agent))

                s_batch += s_batch_agent
                a_batch += a_batch_agent
                r_batch += r_batch_agent
           
            assert len(s_batch)*config.max_moves == len(a_batch)
            #used shared RMSProp, i.e., shared g
            """value_loss, entropy, actor_gradients, critic_gradients = network.actor_critic_train(np.array(s_batch), 
                                                                        np.array(a_batch), 
                                                                        np.array(r_batch).astype(np.float32), 
                                                                        config.entropy_weight)"""
            actions = np.eye(game.action_dim, dtype=np.float32)[np.array(a_batch)]# batch_size * action_dim
            value_loss, policy_loss, entropy, actor_gradients, critic_gradients = network.actor_critic_train(np.array(s_batch), 
                                                                    actions, 
                                                                    np.array(r_batch).astype(np.float32), 
                                                                    config.entropy_weight)
       
            if GRADIENTS_CHECK:
                for g in range(len(actor_gradients)):
                    assert np.any(np.isnan(actor_gradients[g])) == False, ('actor_gradients', s_batch, a_batch, r_batch)
                for g in range(len(critic_gradients)):
                    assert np.any(np.isnan(critic_gradients[g])) == False, ('critic_gradients', s_batch, a_batch, r_batch)

            if step % config.save_step == config.save_step - 1:
                network.save_ckpt(_print=True)
                
                #log training information
                actor_learning_rate = network.lr_schedule(network.actor_optimizer.iterations.numpy()).numpy()
                avg_value_loss = np.mean(value_loss)
                avg_policy_loss = np.mean(policy_loss)
                avg_reward = np.mean(r_batch)
                avg_entropy = np.mean(entropy)
            
                network.inject_summaries({
                    'learning rate': actor_learning_rate,
                    'value loss': avg_value_loss,
                    'policy loss': avg_policy_loss,
                    'avg reward': avg_reward,
                    'avg entropy': avg_entropy
                    }, step)
                print('lr:%f, value loss:%f, avg reward:%f, avg entropy:%f'%(actor_learning_rate, avg_value_loss, avg_reward, avg_entropy))

        elif config.model == 'pure_policy':
            #assemble experiences from the agents
            s_batch = []
            a_batch = []
            r_batch = []
            ad_batch = []

            for i in range(config.num_agents):
                s_batch_agent, a_batch_agent, r_batch_agent, ad_batch_agent = experience_queues[i].get()
              
                assert len(s_batch_agent) == config.num_iter, \
                    (len(s_batch_agent), len(a_batch_agent), len(r_batch_agent), len(ad_batch_agent))

                s_batch += s_batch_agent
                a_batch += a_batch_agent
                r_batch += r_batch_agent
                ad_batch += ad_batch_agent
           
            assert len(s_batch)*config.max_moves == len(a_batch)
            #used shared RMSProp, i.e., shared g
            #entropy, gradients = network.policy_train(np.array(s_batch), np.array(a_batch), np.array(ad_batch).astype(np.float32), config.entropy_weight)
            
            # a_batch is like [[np.array([3]), np.array([5]),...], [np.array([2]), np.array([6]),...], ...]
            # for example, if action_dim=3, a_batch[0]=[np.array([0]), np.array([2]),np.array([1])],
            #then, np.eye(game.action_dim, dtype=np.float32)[np.array(a_batch[i])] would be
            # [[1, 0, 0],
            #  [0, 0, 1],
            #  [0, 1, 0]]
            actions = []
            for i in range(len(a_batch)):
                actions.append(np.eye(game.action_dim, dtype=np.float32)[np.array(a_batch[i])].reshape(-1))
            actions = np.array(actions)
            entropy, gradients, policy_loss = network.policy_train(np.array(s_batch), 
                                                      actions, 
                                                      np.vstack(ad_batch).astype(np.float32), 
                                                      config.entropy_weight)

            if GRADIENTS_CHECK:
                for g in range(len(gradients)):
                    assert np.any(np.isnan(gradients[g])) == False, (s_batch, a_batch, r_batch)
            
            if step % config.save_step == config.save_step - 1:
                network.save_ckpt(_print=True)
                
                #log training information
                learning_rate = network.lr_schedule(network.optimizer.iterations.numpy()).numpy()
                # avg_reward = np.asscalar(np.mean(r_batch))
                # avg_advantage = np.asscalar(np.mean(ad_batch))
                # avg_entropy = np.asscalar(np.mean(entropy))
                # avg_policy_loss = np.asscalar(np.mean(policy_loss))
                avg_reward = np.mean(r_batch)
                avg_advantage = np.mean(ad_batch)
                avg_entropy = np.mean(entropy)
                avg_policy_loss = np.mean(policy_loss)
                network.inject_summaries({
                    'learning rate': learning_rate,
                    'avg reward': avg_reward,
                    'avg advantage': avg_advantage,
                    'avg entropy': avg_entropy,
                    'avg policy loss': avg_policy_loss
                    }, step)
                print('lr:%f, avg reward:%f, avg advantage:%f, avg entropy:%f'%(learning_rate, avg_reward, avg_advantage, avg_entropy))

"""def get_action_prob(logits):
    softmax = []
    max_l = np.max(logits)
    for l in logits:
        softmax.append(math.exp(l-max_l))
    softmax_sum = sum(softmax)
    softmax = np.array(softmax)/softmax_sum
   
    return softmax"""

def agent(agent_id, config, game, tm_subset, model_weights_queue, experience_queue):
    random_state = np.random.RandomState(seed=agent_id)
    network = Network(config, game.input_state_dims, game.action_dim, master=False)

    # initial synchronization of the model weights from the coordinator 
    model_weights = model_weights_queue.get()
    if config.model == 'actor_critic':
        network.model.set_weights(model_weights)
    elif config.model == 'pure_policy':
        network.policy_model.set_weights(model_weights)

    idx = 0
    s_batch = []
    a_batch = []
    r_batch = []
    if config.model == 'pure_policy':
        ad_batch = []
    run_iteration_idx = 0
    num_tms = len(tm_subset)
    # random_state.shuffle(tm_subset)
    run_iterations = config.num_iter
    
    while True:
        tm_idx = tm_subset[idx]
        #state
        state = game.get_state(tm_idx) 
        s_batch.append(state) # append first then expand dims, because model need ndim=3
        state = np.expand_dims(state, 0)
        # s_batch.append(state)
        #action
        if config.model == 'actor_critic':    
            policy = network.actor_predict(state)
            policy = policy.numpy()
            policy = np.squeeze(policy)
        elif config.model == 'pure_policy':
            policy = network.policy_predict(state)
            policy = policy.numpy()
            policy = np.squeeze(policy)
        assert np.count_nonzero(policy) >= config.max_moves, (policy, state)
        actions = []
        for i in range(config.critical_num + 1):
            # assert np.abs(np.sum(policy[config.action_dim*i:config.action_dim*(i+1)]) - 1 ) < 0.001
            actions.append(random_state.choice(game.action_dim, 1, p=policy[config.action_dim*i:config.action_dim*(i+1)], replace=False))
        # print(actions)
        a_batch.append(actions)

        #reward
        
        reward = game.reward(tm_idx, actions)
        r_batch.append(reward)

        if config.model == 'pure_policy':
            #advantage
            if config.baseline == 'avg':
                ad_batch.append(game.advantage(tm_idx, reward))
                game.update_baseline(tm_idx, reward)
            elif config.baseline == 'best':
                best_actions = policy.argsort()[-game.max_moves:]
                best_reward = game.reward(tm_idx, best_actions)
                ad_batch.append(reward - best_reward)

        run_iteration_idx += 1
        if run_iteration_idx >= run_iterations:
            # Report experience to the coordinator                          
            # instead of reporting gradients to the coordiantor
            if config.model == 'actor_critic':    
                experience_queue.put([s_batch, a_batch, r_batch])
            elif config.model == 'pure_policy':
                experience_queue.put([s_batch, a_batch, r_batch, ad_batch])
            
            #print('report', agent_id)

            # synchronize the network parameters from the coordinator
            model_weights = model_weights_queue.get()
            if config.model == 'actor_critic':
                network.model.set_weights(model_weights)
            elif config.model == 'pure_policy':
                network.policy_model.set_weights(model_weights)
            
            del s_batch[:]
            del a_batch[:]
            del r_batch[:]
            if config.model == 'pure_policy':
                del ad_batch[:]
            run_iteration_idx = 0
       
        # Update idx
        idx += 1
        # if idx == num_tms:
        if idx == num_tms - 1:# the current action is for the next state, so the last state do not have reward
        #    game.random_state.shuffle(tm_subset) # we can't shuffle
           idx = 0


def main(_):
    config = get_config(FLAGS) or FLAGS
    print(config.num_iter)
    env = Environment(config, is_training=True)
    game = Game(config, env)
    model_weights_queues = []
    experience_queues = []
    if FLAGS.num_agents == 0:
        config.num_agents = mp.cpu_count() - 1
    #FLAGS.num_iter = env.tm_cnt//config.num_agents
    print('agent num: %d, iter num: %d\n'%(config.num_agents, FLAGS.num_iter))
    for _ in range(config.num_agents):
        model_weights_queues.append(mp.Queue(1))
        experience_queues.append(mp.Queue(1))

    tm_set = np.arange(env.tm_cnt - env.tm_history + 1)
    tm_subsets = np.array_split(tm_set, config.num_agents)

    coordinator = mp.Process(target=central_agent, args=(config, game, model_weights_queues, experience_queues))

    coordinator.start()

    agents = []
    for i in range(config.num_agents):
        agents.append(mp.Process(target=agent, args=(i, config, game, tm_subsets[i], model_weights_queues[i], experience_queues[i])))

    for i in range(config.num_agents):
        agents[i].start() ##UnparsedFlagAccessError: Trying to access flag --num_iter before flags were parsed.

    for i in range(config.num_agents):
        agents[i].join()

    coordinator.join()

if __name__ == '__main__':
    app.run(main)
