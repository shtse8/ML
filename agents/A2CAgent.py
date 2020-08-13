import os
import __main__
import random
import numpy as np
import sys
import collections
import math
import time
from memories.PrioritizedMemory import PrioritizedMemory
from memories.SimpleMemory import SimpleMemory
from memories.Transition import Transition
from .Agent import Agent

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import torchvision.transforms as T


def set_init(layers):
    for layer in layers:
        nn.init.normal_(layer.weight, mean=0., std=0.1)
        nn.init.constant_(layer.bias, 0.)

class Net(nn.Module):
    def __init__(self, s_dim, a_dim):
        super(Net, self).__init__()
        self.s_dim = s_dim
        self.a_dim = a_dim
        self.pi1 = nn.Linear(s_dim, 128)
        self.pi2 = nn.Linear(128, a_dim)
        self.v1 = nn.Linear(s_dim, 128)
        self.v2 = nn.Linear(128, 1)

        set_init([self.pi1, self.pi2, self.v1, self.v2])
        self.distribution = torch.distributions.Categorical
        
    def forward(self, x):
        pi1 = torch.tanh(self.pi1(x))
        logits = self.pi2(pi1)
        v1 = torch.tanh(self.v1(x))
        values = self.v2(v1)
        return logits, values

    def choose_action(self, s):
        self.eval()
        logits, _ = self.forward(s)
        prob = F.softmax(logits, dim=1).data
        m = self.distribution(prob)
        return m.sample().numpy()[0]

    def loss_func(self, s, a, v_t):
        self.train()
        logits, values = self.forward(s)
        td = v_t - values
        c_loss = td.pow(2)
        
        probs = F.softmax(logits, dim=1)
        m = self.distribution(probs)
        exp_v = m.log_prob(a) * td.detach().squeeze()
        a_loss = -exp_v
        total_loss = (c_loss + a_loss).mean()
        return total_loss

class A2CAgent(Agent):
    def __init__(self, env, **kwargs):
        super().__init__(env, **kwargs)
        
        # Trainning
        self.weights_path = kwargs.get('weights_path', "./weights/" + os.path.basename(__main__.__file__) + ".h5")
        self.learning_rate = kwargs.get('learning_rate', .001)
        self.gamma = kwargs.get('gamma', 0.99)
        
        # Memory
        self.memory_size = kwargs.get('memory_size', 100000)
        
        # self.ltmemory = collections.deque(maxlen=self.memory_size)
        self.memory = SimpleMemory(self.memory_size)
        
        # Prediction model (the main Model)
        self.model = Net(np.product(self.env.observationSpace), self.env.actionSpace)
        # self.model.cuda()
        # self.optimizer = optim.RMSprop(self.model.parameters(), lr=self.learning_rate)
        self.optimizer = optim.Adam(self.model.parameters(), lr=self.learning_rate)
        
        self.target_update_counter = 0

    def beginPhrase(self):
        self.memory.clear()
        return super().beginPhrase()
    
    def printSummary(self):
        print(self.model)

    def commit(self, transition: Transition):
        self.memory.add(transition)
        super().commit(transition)
        
    def update_epsilon(self):
        if self.epsilon > self.epsilon_min:
            self.epsilon -= self.epsilon_decay
            self.epsilon = max(self.epsilon_min, self.epsilon)
    
    def getAction(self, state, actionMask = None):
        self.model.eval()
        stateTensor = torch.tensor(state, dtype=torch.float).view(1, -1)
        # stateTensor = stateTensor.cuda()
        logits, _ = self.model(stateTensor)
        # logits = logits.detach().cpu()
        prob = F.softmax(logits, dim=1).data
        prob *= actionMask
        m = self.model.distribution(prob)
        # print(m, m.sample())
        return m.sample().numpy()[0]
    
    def endEpisode(self):
        if self.isTraining():
            self.learn()
        super().endEpisode()
      
    def v_wrap(np_array, dtype=np.float32):
        if np_array.dtype != dtype:
            np_array = np_array.astype(dtype)
        return torch.from_numpy(np_array)

    def learn(self):
        self.model.train()
        
        batch = self.memory
        
        states = np.array([x.state for x in batch])
        states = torch.tensor(states, dtype=torch.float).view(states.shape[0], -1) #.cuda()
        
        actions = np.array([x.action for x in batch])
        actions = torch.tensor(actions, dtype=torch.long) #.cuda()
        
        rewards = np.array([x.reward for x in batch])
        # rewards = torch.tensor(rewards, dtype=torch.float)
        
        # dones = np.array([x.done for x in batch])
        # dones = torch.tensor(dones, dtype=torch.float)
                
        # nextStates = np.array([x.nextState for x in batch])
        # nextStates = torch.tensor(nextStates, dtype=torch.float).view(nextStates.shape[0], -1)
        

        # if done:
        #     v_s_ = 0.               # terminal
        # else:
        #     v_s_ = lnet.forward(v_wrap(s_[None, :]))[-1].data.numpy()[0, 0]
        v_s_ = 0
        buffer_v_target = []
        # print(rewards, rewards[::-1])
        for r in rewards[::-1]:    # reverse buffer r
            v_s_ = r + self.gamma * v_s_
            buffer_v_target.append(v_s_)
        buffer_v_target.reverse()
        buffer_v_target = torch.tensor(buffer_v_target, dtype=torch.float) #.cuda()
        # print(buffer_v_target)
        
        
        loss = self.model.loss_func(states, actions, buffer_v_target)
        
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()
        
        self.memory.clear()
            
        self.lossHistory.append(loss.item())
    
    def save(self):
        try:
            torch.save(self.model.state_dict(), self.weights_path)
            # print("Saved Weights.")
        except:
            print("Failed to save.")
        
    def load(self):
        try:
            # self.model.load_weights(self.weights_path)
            self.model.load_state_dict(torch.load(self.weights_path))
            print("Weights loaded.")
        except:
            print("Failed to load.")
