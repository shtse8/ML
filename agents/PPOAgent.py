import asyncio
import __main__
import types
from pathlib import Path
import os
import math
import torch.multiprocessing as mp
import torch.optim.lr_scheduler as schedular
import torch.nn.functional as F
import torch.optim as optim
import torch.nn as nn
import torch
import utils.Function as Function
from utils.multiprocessing import Proxy
from utils.PredictionHandler import PredictionHandler
from .Agent import Agent
from memories.Transition import Transition
from memories.SimpleMemory import SimpleMemory
from games.GameFactory import GameFactory
import collections
import numpy as np
from enum import Enum
import time
import sys
from multiprocessing.managers import NamespaceProxy, SyncManager
import multiprocessing.connection
from utils.PipedProcess import Process, PipedProcess


# def init_layer(m):
#     weight = m.weight.data
#     weight.normal_(0, 1)
#     weight *= 1.0 / torch.sqrt(weight.pow(2).sum(1, keepdim=True))
#     nn.init.constant_(m.bias.data, 0)
#     return m


class Message:
    def __init__(self):
        pass


class NetworkInfo(Message):
    def __init__(self, stateDict, version):
        self.stateDict = stateDict
        self.version = version


class LearnReport(Message):
    def __init__(self, loss=0, steps=0, drops=0):
        self.loss = loss
        self.steps = steps
        self.drops = drops


class EnvReport(Message):
    def __init__(self):
        self.rewards = 0


class Action(object):
    def __init__(self, index, mask, prediction):
        self.index = index
        self.mask = mask
        self.prediction = prediction

    def __int__(self):
        return self.index

    @property
    def log(self):
        # https://github.com/pytorch/pytorch/blob/master/torch/distributions/utils.py#L72
        eps = torch.finfo(torch.float).eps
        prob = np.array([p if self.mask[i] else 0 for i, p in enumerate(self.prediction)])
        prob = prob / prob.sum()
        prob = min(1-eps, max(eps, self.prediction[self.index]))
        return math.log(prob)


class Epoch(Message):
    def __init__(self, ):
        self.num = 0
        self.target_episodes = 0
        self.steps: int = 0
        self.drops: int = 0
        self.rewards: float = 0
        self.total_loss: float = 0
        self.epoch_start_time: int = 0
        self.epoch_end_time: int = 0
        self.episodes = 0

        # for stats
        # self.history = collections.deque(maxlen=target_episodes)
        self.bestRewards = -math.inf
        self.totalRewards = 0
        self.envs = 0

    def start(self, target_episodes):
        self.epoch_start_time = time.perf_counter()
        self.target_episodes = target_episodes
        self.steps = 0
        self.drops = 0
        self.rewards = 0
        self.total_loss = 0
        self.epoch_start_time = 0
        self.epoch_end_time = 0
        self.episodes = 0
        self.bestRewards = -math.inf
        self.totalRewards = 0
        self.envs = 0
        self.num += 1
        return self

    def restart(self):
        return self.start(self.target_episodes)

    def end(self):
        self.epoch_end_time = time.perf_counter()
        return self

    @property
    def hitRate(self):
        return self.steps / (self.steps + self.drops) if (self.steps + self.drops) > 0 else math.nan

    @property
    def isEnd(self):
        return self.epoch_end_time > 0

    @property
    def progress(self):
        return self.episodes / self.target_episodes

    @property
    def duration(self):
        return (self.epoch_end_time if self.epoch_end_time > 0 else time.perf_counter()) - self.epoch_start_time

    @property
    def loss(self):
        return self.total_loss / self.steps if self.steps > 0 else 0

    @property
    def durationPerEpisode(self):
        return self.duration / self.episodes if self.episodes > 0 else math.inf

    @property
    def estimateDuration(self):
        return self.target_episodes * self.durationPerEpisode

    @property
    def avgRewards(self):
        return self.totalRewards / self.envs if self.envs > 0 else math.nan

    def add(self, report: EnvReport):
        if report.rewards > self.bestRewards:
            self.bestRewards = report.rewards
        self.totalRewards += report.rewards
        self.envs += 1
        # self.history.append(report)
        return self

    def trained(self, loss, steps):
        self.total_loss += loss * steps
        self.steps += steps
        self.episodes += 1
        if self.episodes >= self.target_episodes:
            self.end()
        return self


class ConvLayers(nn.Module):
    def __init__(self, inputShape, n_outputs):
        super().__init__()
        if min(inputShape[1], inputShape[2]) < 8:
            # small CNN
            self.layers = nn.Sequential(
                nn.Conv2d(inputShape[0], 16, kernel_size=3, stride=1, padding=1),
                nn.ReLU(),
                nn.Conv2d(16, 32, kernel_size=3, stride=1, padding=1),
                nn.ReLU(),
                nn.Flatten(),
                nn.Linear(32 * inputShape[1] * inputShape[2], n_outputs),
                nn.ReLU())
        else:
            self.layers = nn.Sequential(
                # [C, H, W] -> [32, H, W]
                nn.Conv2d(inputShape[0], 32, kernel_size=8, stride=4),
                nn.ReLU(),
                nn.Conv2d(32, 64, kernel_size=4, stride=2),
                nn.ReLU(),
                nn.Conv2d(64, 64, kernel_size=3, stride=1),
                nn.ReLU(),
                # [64, H, W] -> [64 * H * W]
                nn.Flatten(),
                nn.Linear(64 * inputShape[1] * inputShape[2], n_outputs),
                nn.ReLU())

    def forward(self, x):
        # x = x.permute(0, 3, 1, 2)  # [B, H, W, C] => [B, C, H, W]
        return self.layers(x)


class FCLayers(nn.Module):
    def __init__(self, n_inputs, n_outputs, num_layers=1, hidden_nodes=128):
        super().__init__()
        self.layers = nn.ModuleList()
        for i in range(num_layers):
            in_nodes = n_inputs if i == 0 else hidden_nodes
            out_nodes = n_outputs if i == num_layers - 1 else hidden_nodes
            self.layers.append(nn.Linear(in_nodes, out_nodes))
            self.layers.append(nn.Tanh())

    def forward(self, x):
        for m in self.layers:
            x = m(x)
        return x


class BodyLayers(nn.Module):
    def __init__(self, inputShape, n_outputs, hidden_nodes=128):
        super().__init__()
        if type(inputShape) is tuple and len(inputShape) == 3:
            self.layers = ConvLayers(inputShape, n_outputs)
        else:
            if type(inputShape) is tuple and len(inputShape) == 1:
                inputShape = inputShape[0]
            self.layers = FCLayers(inputShape, n_outputs, hidden_nodes=hidden_nodes)

    def forward(self, x):
        return self.layers(x)


class Network(nn.Module):
    def __init__(self, inputShape, n_outputs, name="network"):
        super().__init__()
        self.name = name
        self.optimizer = None
        self.version: int = 1
        self.info: NetworkInfo = None

    def buildOptimizer(self):
        raise NotImplementedError

    def _updateStateDict(self):
        if self.info is None or self.info.version != self.version:
            # print("Update Cache", self.version)
            stateDict = self.state_dict()
            for key, value in stateDict.items():
                stateDict[key] = value.cpu()  #.detach().numpy()
            self.info = NetworkInfo(stateDict, self.version)

    def getInfo(self) -> NetworkInfo:
        self._updateStateDict()
        return self.info

    def loadInfo(self, info: NetworkInfo):
        stateDict = info.stateDict
        # for key, value in stateDict.items():
        #     stateDict[key] = torch.from_numpy(value)
        self.load_state_dict(stateDict)
        self.version = info.version

    def isNewer(self, info: NetworkInfo):
        return info.version > self.version


class GRULayers(nn.Module):
    def __init__(self, n_inputs, n_outputs, num_layers=1):
        super().__init__()
        self.n_outputs = n_outputs
        self.gru = nn.GRU(n_inputs, n_outputs, num_layers=num_layers)

    def getInitHiddenState(self, device):
        return torch.zeros(self.n_outputs, device=device)

    def forward(self, x, h):
        # (B, N) -> (1, B, N)
        x, h = self.gru(x.unsqueeze(0), h.unsqueeze(0))
        return x.squeeze(0), h.squeeze(0)


class PPONetwork(Network):
    def __init__(self, inputShape, n_outputs):
        super().__init__(inputShape, n_outputs)

        hidden_nodes = 128
        # semi_hidden_nodes = hidden_nodes // 2
        self.body = BodyLayers(inputShape, hidden_nodes)

        self.gru = GRULayers(hidden_nodes, hidden_nodes, num_layers=1)

        # Define policy head
        self.policy = nn.Sequential(
            nn.Linear(hidden_nodes, n_outputs),
            nn.Softmax(dim=-1))

        # Define value head
        self.value = nn.Linear(hidden_nodes, 1)

            
    def buildOptimizer(self, learningRate):
        self.optimizer = optim.Adam(self.parameters(), lr=learningRate)
        return self

    def getInitHiddenState(self, device):
        return self.gru.getInitHiddenState(device)

    def _body(self, x, hiddenStates = None):
        x = self.body(x)
        x, hiddenStates = self.gru(x, hiddenStates)
        return x, hiddenStates

    def forward(self, x, hiddenStates = None):
        x, hiddenStates = self._body(x, hiddenStates)
        return self.policy(x), self.value(x), hiddenStates

    def getPolicy(self, x, hiddenStates = None):
        x, hiddenStates = self._body(x, hiddenStates)
        return self.policy(x), hiddenStates

    def getValue(self, x, hiddenStates = None):
        x, hiddenStates = self._body(x, hiddenStates)
        return self.value(x), hiddenStates


class Config:
    def __init__(self, sampleSize=512, batchSize=32, learningRate=3e-4):
        self.sampleSize = sampleSize
        self.batchSize = batchSize
        self.learningRate = learningRate

class PPOConfig(Config):
    def __init__(self, sampleSize=512, batchSize=32, learningRate=3e-4, gamma=0.99, epsClip=0.2, gaeCoeff=0.95):
        super().__init__(sampleSize, batchSize, learningRate)
        self.gamma = gamma
        self.epsClip = epsClip
        self.gaeCoeff = gaeCoeff

class Algo:
    def __init__(self, name, config: Config):
        self.name = name
        self.config = config
        self.device = torch.device(
            "cuda:0" if torch.cuda.is_available() else "cpu")

    def createNetwork(self) -> Network:
        raise NotImplementedError

    def getAction(self, network, state, mask, isTraining: bool) -> Action:
        raise NotImplementedError

    def learn(self, network: Network, memory):
        raise NotImplementedError


class PPOAlgo(Algo):
    def __init__(self, config=PPOConfig(
        sampleSize=256,
        batchSize=256
    )):
        super().__init__("PPO", config)

    def createNetwork(self, inputShape, n_outputs) -> Network:
        return PPONetwork(inputShape, n_outputs)

    def getAction(self, network, state, mask, isTraining: bool, hiddenState = None) -> Action:
        network.eval()
        with torch.no_grad():
            stateTensor = torch.tensor([state], dtype=torch.float, device=self.device)
            maskTensor = torch.tensor([mask], dtype=torch.bool, device=self.device)
            prediction, nextHiddenState = network.getPolicy(stateTensor, hiddenState.unsqueeze(0))
            prediction = prediction.masked_fill(~maskTensor, 0)
            prediction = prediction / prediction.sum()
            prediction = prediction.squeeze(0)
            dist = torch.distributions.Categorical(probs=prediction)
            index = dist.sample() if isTraining else dist.mode()
            return Action(
                index=index.item(),
                mask=mask,
                prediction=prediction.cpu().detach().numpy()
            ), nextHiddenState.squeeze(0)

    def processAdvantage(self, network, memory):
        with torch.no_grad():
            lastValue = 0
            lastMemory = memory[-1]
            if not lastMemory.done:
                lastState = torch.tensor([lastMemory.nextState], dtype=torch.float, device=self.device)
                hiddenState = torch.tensor([lastMemory.nextHiddenState], dtype=torch.float, device=self.device)
                lastValue, _ = network.getValue(lastState, hiddenState)
                lastValue = lastValue.item()

            states = np.array([x.state for x in memory])
            states = torch.tensor(states, dtype=torch.float, device=self.device)

            hiddenState = np.array([x.hiddenState for x in memory])
            hiddenState = torch.tensor(hiddenState, dtype=torch.float, device=self.device).detach()
            values, _ = network.getValue(states, hiddenState)
            values = values.squeeze(1).cpu().detach().numpy()

            # GAE (General Advantage Estimation)
            # Paper: https://arxiv.org/abs/1506.02438
            # Code: https://github.com/openai/baselines/blob/master/baselines/ppo2/runner.py#L55-L64
            gae = 0
            for i in reversed(range(len(memory))):
                transition = memory[i]
                detlas = transition.reward + self.config.gamma * lastValue * (1 - transition.done) - values[i]
                gae = detlas + self.config.gamma * self.config.gaeCoeff * gae * (1 - transition.done)
                # from baseline
                # https://github.com/openai/baselines/blob/master/baselines/ppo2/runner.py#L65
                transition.advantage = gae
                transition.reward = gae + values[i]
                transition.value = values[i]
                lastValue = values[i]

            
            # Normalize advantages
            # https://github.com/openai/baselines/blob/master/baselines/ppo2/model.py#L139
            advantages = np.array([x.advantage for x in memory])
            advantages = Function.normalize(advantages)
            for transition, advantage in zip(memory, advantages):
                transition.advantage = advantage

    def getGAE(self, rewards, dones, values, lastValue=0):
        advantages = np.zeros_like(rewards).astype(float)
        gae = 0
        for i in reversed(range(len(rewards))):
            detlas = rewards[i] + self.config.gamma * \
                lastValue * (1 - dones[i]) - values[i]
            gae = detlas + self.config.gamma * self.gaeCoeff * gae * (1 - dones[i])
            advantages[i] = gae
            lastValue = values[i]
        return advantages

    def learn(self, network: Network, memory):
        network.train()
        memory = np.array(memory)
        n_miniBatch = len(memory) // self.config.batchSize
        totalLoss = 0
        network.optimizer.zero_grad()
        for i in range(n_miniBatch):
            startIndex = i * self.config.batchSize
            endIndex = startIndex + self.config.batchSize
            minibatch = memory[startIndex:endIndex]
            
            states = np.array([x.state for x in minibatch])
            states = torch.tensor(states, dtype=torch.float, device=self.device).detach()

            actions = np.array([x.action.index for x in minibatch])
            actions = torch.tensor(actions, dtype=torch.long, device=self.device).detach()

            masks = np.array([x.action.mask for x in minibatch])
            masks = torch.tensor(masks, dtype=torch.bool, device=self.device).detach()

            old_log_probs = np.array([x.action.log for x in minibatch])
            old_log_probs = torch.tensor(old_log_probs, dtype=torch.float, device=self.device).detach()

            returns = np.array([x.reward for x in minibatch])
            returns = torch.tensor(returns, dtype=torch.float, device=self.device).detach()

            old_values = np.array([x.value for x in minibatch])
            old_values = torch.tensor(old_values, dtype=torch.float, device=self.device).detach()

            # advantages = returns - old_values
            advantages = np.array([x.advantage for x in minibatch])
            advantages = torch.tensor(advantages, dtype=torch.float, device=self.device).detach()

            hiddenStates = np.array([x.hiddenState for x in minibatch])
            hiddenStates = torch.tensor(hiddenStates, dtype=torch.float, device=self.device).detach()
            probs, values, hiddenStates = network(states, hiddenStates)
            values = values.squeeze(1)

            # mask probs
            probs = probs.masked_fill(~masks, 0)
            probs = probs / probs.sum()

            # PPO2 - Confirm the samples aren't too far from pi.
            # porb1 / porb2 = exp(log(prob1) - log(prob2))
            dist = torch.distributions.Categorical(probs=probs)
            ratios = torch.exp(dist.log_prob(actions) - old_log_probs)
            # print(ratios)
            policy_losses1 = ratios * advantages
            policy_losses2 = ratios.clamp(1 - self.config.epsClip, 1 + self.config.epsClip) * advantages

            # Maximize Policy Loss (Rewards)
            policy_loss = -torch.min(policy_losses1, policy_losses2).mean()

            # Maximize Entropy Loss
            entropy_loss = -dist.entropy().mean()
            
            # Minimize Value Loss  (MSE)
            # Clip the value to reduce variability during Critic training
            # https://github.com/openai/baselines/blob/master/baselines/ppo2/model.py#L66-L75
            # https://github.com/ikostrikov/pytorch-a2c-ppo-acktr-gail/blob/master/a2c_ppo_acktr/algo/ppo.py#L69-L75
            value_loss1 = (returns - values).pow(2)
            valuesClipped = old_values + torch.clamp(values - old_values, -self.config.epsClip, self.config.epsClip)
            value_loss2 = (returns - valuesClipped).pow(2)
            value_loss = 0.5 * torch.max(value_loss1, value_loss2).mean()

            # MSE Loss
            # value_loss = (returns - values).pow(2).mean()

            # Calculating Total loss
            # Wondering  if we need to divide the number of minibatches to keep the same learning rate?
            # As the learning rate is a parameter of optimizer, and only one step is called. 
            # Should be fine to not dividing the number of minibatches.
            weight = len(minibatch) / len(memory)
            loss = (policy_loss + 0.01 * entropy_loss + 0.5 * value_loss) * weight

            # Accumulating the loss to the graph
            loss.backward()
            totalLoss += loss.item()

        # Chip grad with norm
        # https://github.com/openai/baselines/blob/9b68103b737ac46bc201dfb3121cfa5df2127e53/baselines/ppo2/model.py#L107
        nn.utils.clip_grad.clip_grad_norm_(network.parameters(), 0.5)
  
        network.optimizer.step()
        network.version += 1

        return totalLoss


class Trainer:
    def __init__(self, algo: Algo, gameFactory: GameFactory, sync):
        self.algo = algo
        self.gameFactory = gameFactory
        self.algo.device = sync.getDevice()
        self.weightPath = "./weights/"
        self.sync = sync
        self.evaluators = []
        self.network = None
        self.networks = []
        self.epoch = None
        self.lastSave = 0

    def learn(self, memory):
        steps = len(memory)
        loss = self.algo.learn(self.network, memory)
        # learn report handling
        self.sync.epoch.trained(loss, steps)
        self.sync.totalEpisodes.value += 1
        self.sync.totalSteps.value += steps
        if self.sync.epoch.isEnd:
            # self.update(0)
            print()
            # reset epoch
            self.sync.epoch.restart()

        # update sync
        # self.sync.epoch.value = self.epoch

    def pushNewNetwork(self):
        networkInfo = self.network.getInfo()
        if networkInfo.version > self.sync.latestVersion.value:
            self.sync.latestStateDict.update(networkInfo.stateDict)
            self.sync.latestVersion.value = networkInfo.version

    async def start(self, episodes=1000, load=False):
        
        env = self.gameFactory.get()
        self.network = self.algo.createNetwork(env.observationShape, env.actionSpace)
        self.networks.append(self.network)
        if load:
            self.load()

        evaluators = []
        n_workers = max(torch.cuda.device_count(), 1)
        for i in range(n_workers):
            evaluator = EvaluatorService(self.network, self.algo, self.gameFactory, self.sync).start()
            evaluators.append(evaluator)

        self.evaluators = np.array(evaluators)

        self.network = self.network.buildOptimizer(self.algo.config.learningRate).to(self.algo.device)
        n_samples = self.algo.config.sampleSize * n_workers
        evaulator_samples = self.algo.config.sampleSize

        self.sync.epoch.start(episodes)
        self.lastSave = time.perf_counter()
        loop = asyncio.get_running_loop()
        loop.create_task(self.reportQueueHandling())
        while True:
            # push new network
            self.pushNewNetwork()
            # collect samples
            memory = collections.deque(maxlen=n_samples)
            promises = np.array([x.call("roll", (evaulator_samples,)) for x in self.evaluators])
            # https://docs.python.org/3/library/asyncio-task.html#asyncio.as_completed
            for promise in asyncio.as_completed(promises):
                response = await promise  # earliest result
                # print("Rolled Memory: ", len(response.result))
                memory.extend(response.result)

            # learn
            self.learn(memory)
                    
            if time.perf_counter() - self.lastSave > 60:
                self.save()

    async def reportQueueHandling(self):
        while True:
            if not self.sync.reportQueue.empty():
                message = self.sync.reportQueue.get()
                if isinstance(message, EnvReport):
                    self.sync.epoch.add(message)
            await asyncio.sleep(0)

    def save(self) -> None:
        try:
            path = self.getSavePath(True)
            data = {
                "totalSteps": self.sync.totalSteps.value,
                "totalEpisodes": self.sync.totalEpisodes.value
            }
            for network in self.networks:
                data[network.name] = network.state_dict()
            torch.save(data, path)
            self.lastSave = time.perf_counter()
            # print("Saved Weights.")
        except Exception as e:
            print("Failed to save.", e)
        
    def load(self) -> None:
        try:
            path = self.getSavePath()
            print("Loading from path: ", path)
            data = torch.load(path, map_location='cpu')
            # data = torch.load(path, map_location=self.device)
            self.sync.totalSteps.value = int(data["totalSteps"]) if "totalSteps" in data else 0
            self.sync.totalEpisodes.value = int(data["totalEpisodes"]) if "totalEpisodes" in data else 0
            for network in self.networks:
                print(f"{network.name} weights loaded.")
                network.load_state_dict(data[network.name])
        except Exception as e:
            print("Failed to load.", e)
    
    def getSavePath(self, makeDir: bool = False) -> str:
        path = os.path.join(self.weightPath, self.algo.name.lower(), self.gameFactory.name + ".h5")
        if makeDir:
            Path(os.path.dirname(path)).mkdir(parents=True, exist_ok=True)
        return path

class Evaluator:
    def __init__(self, network, algo: Algo, gameFactory, sync):
        self.algo = algo
        self.gameFactory = gameFactory
        self.env = gameFactory.get()
        # self.algo.device = torch.device("cpu")
        self.algo.device = sync.getDevice()
        self.network = network.to(self.algo.device)
        self.network.version = -1
        self.sync = sync

        self.playerCount = self.env.getPlayerCount()
        self.agents = np.array([Agent(i + 1, self.env, network, algo) for i in range(self.playerCount)])
        self.started = False

    def updateNetwork(self):
        if self.network.version < self.sync.latestVersion.value:
            networkInfo = NetworkInfo(self.sync.latestStateDict, self.sync.latestVersion.value)
            self.network.loadInfo(networkInfo)

    def loop(self, num):
        # auto reset
        if not self.started:
            self.env.reset()
            self.started = True
        elif self.env.isDone():
            for agent in self.agents:
                report = agent.done()
                self.sync.reportQueue.put(report)
            self.env.reset()
        
        # memoryCount = min([len(x.memory) for x in self.agents])
        memoryCount = sum([len(x.memory) for x in self.agents])
        return memoryCount < num

    def roll(self, num):
        self.updateNetwork()
        self.generateTransitions(num)
        memory = []
        for agent in self.agents:
            self.algo.processAdvantage(self.network, agent.memory)
            memory.extend(agent.memory)
        return np.array(memory)

    def generateTransitions(self, num):
        for agent in self.agents:
            agent.resetMemory(num)
        while self.loop(num):
            for agent in self.agents:
                agent.step()


class Agent:
    def __init__(self, id, env, network, algo):
        self.id = id
        self.env = env
        self.memory = None
        self.report = EnvReport()
        self.network = network
        self.algo = algo
        self.player = self.env.getPlayer(self.id)
        self.hiddenState = self.network.getInitHiddenState(self.algo.device)

    def step(self) -> None:
        if not self.env.isDone() and self.player.canStep():
            state = self.player.getState()
            mask = self.player.getMask(state)
            hiddenState = self.hiddenState
            action, nextHiddenState = self.algo.getAction(self.network, state, mask, True, hiddenState)
            nextState, reward, done = self.player.step(action.index)
            transition = Transition(
                state=state, 
                hiddenState=hiddenState.cpu().detach().numpy(), 
                action=action, 
                reward=reward, 
                nextState=nextState, 
                nextHiddenState=nextHiddenState.cpu().detach().numpy(),
                done=done)
            self.hiddenStates = nextHiddenState
            self.memory.append(transition)
            # action reward
            self.report.rewards += transition.reward

    def done(self):
        report = self.report

        # game episode reward
        doneReward = self.player.getDoneReward()

        # set last memory to done, as we may not be the last one to take action.
        # do nothing if last memory has been processed.
        if len(self.memory) > 0:
            lastMemory = self.memory[-1]
            lastMemory.done = True
            lastMemory.reward += doneReward
        report.rewards += doneReward
        
        # reset env variables
        self.hiddenState = self.network.getInitHiddenState(self.algo.device)
        self.report = EnvReport()

        return report

    def resetMemory(self, num):
        self.memory = collections.deque(maxlen=num)


class RL:
    def __init__(self, algo: Algo, gameFactory: GameFactory):
        self.algo = algo
        self.gameFactory = gameFactory

        self.lastPrint = 0
        self.networks = []

        mp.set_start_method("spawn")
        self.sync = SyncContext()

    async def run(self, train: bool = True, load: bool = False, episodes: int = 1000, delay: float = 0) -> None:
        self.delay = delay
        self.isTraining = train
        self.lastSave = time.perf_counter()
        self.workingPath = os.path.dirname(__main__.__file__)
        # multiprocessing.connection.BUFSIZE = 2 ** 24

        trainer = TrainerProcess(self.algo, self.gameFactory, self.sync, episodes, load).start()
        print(f"Train: {self.isTraining}, Trained: {Function.humanize(self.sync.totalEpisodes.value)} episodes, {Function.humanize(self.sync.totalSteps.value)} steps")
        
        while True:
            self.update()
            await asyncio.sleep(0.01)

    def update(self, freq=.1) -> None:
        if time.perf_counter() - self.lastPrint < freq:
            return
        epoch = self.sync.epoch
        if epoch is not None:
            print(f"#{epoch.num} {Function.humanize(epoch.episodes):>6} {epoch.hitRate:>7.2%} | " +
                f'Loss: {Function.humanize(epoch.loss):>6}/ep | ' +
                f'Env: {Function.humanize(epoch.envs):>6} | ' +
                f'Best: {Function.humanize(epoch.bestRewards):>6}, Avg: {Function.humanize(epoch.avgRewards):>6} | ' +
                f'Steps: {Function.humanize(epoch.steps / epoch.duration):>6}/s | Episodes: {1 / epoch.durationPerEpisode:>6.2f}/s | ' +
                f' {Function.humanizeTime(epoch.duration):>5} > {Function.humanizeTime(epoch.estimateDuration):}' +
                '      ',
                end="\b\r")
        self.lastPrint = time.perf_counter()



class MethodCallRequest(Message):
    def __init__(self, method, args):
        self.method = method
        self.args = args


class MethodCallResult(Message):
    def __init__(self, result):
        self.result = result

class Promise:
    def __init__(self):
        self.result = None

class Service(PipedProcess):
    def __init__(self, factory):
        super().__init__()
        self.factory = factory
        self.isRunning = True

    async def asyncRun(self, conn):
        # print("Evaluator", os.getpid(), conn)
        self.object = self.factory()
        while self.isRunning:
            if conn.poll():
                message = conn.recv()
                if isinstance(message, MethodCallRequest):
                    # print("MMethodCallRequest", message.method)
                    result = getattr(self.object, message.method)(*message.args)
                    conn.send(MethodCallResult(result))
            await asyncio.sleep(0)

    async def waitResponse(self, future):
        while not self.poll():
            await asyncio.sleep(0)
        message = self.recv()
        future.set_result(message)

    def call(self, method, args=()):
        # print("Call", method)
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.send(MethodCallRequest(method, args))
        loop.create_task(self.waitResponse(future))
        return future

class EvaluatorService(Service):
    def __init__(self, network, algo, gameFactory, sync):
        self.network = network
        self.algo = algo
        self.gameFactory = gameFactory
        self.sync = sync
        super().__init__(self.factory)

    def factory(self):
        return Evaluator(self.network, self.algo, self.gameFactory, self.sync)

class TrainerProcess(Process):
    def __init__(self, algo, gameFactory, sync, episodes, load):
        super().__init__()
        self.algo = algo
        self.gameFactory = gameFactory
        self.sync = sync
        self.episodes = episodes
        self.load = load

    async def asyncRun(self):
        # print("Trainer", os.getpid())
        await Trainer(self.algo, self.gameFactory, self.sync).start(self.episodes, self.load)



# thread safe
class SyncContext:
    ProxyEpoch = Proxy(Epoch)

    def __init__(self):
        # manager = mp.Manager()
        SyncManager.register('Epoch', Epoch, self.ProxyEpoch)
        manager = SyncManager()
        manager.start()
        
        self.latestStateDict = manager.dict()
        self.latestVersion = manager.Value('i', -1)
        self.memoryQueue = manager.Queue(maxsize=1000)
        self.reportQueue = manager.Queue(maxsize=1000)
        self.deviceIndex = manager.Value('i', 0)
        self.totalEpisodes = manager.Value('i', 0)
        self.totalSteps = manager.Value('i', 0)
        self.epoch = manager.Epoch()

    def getDevice(self):
        deviceName = "cpu"
        if torch.cuda.is_available():
            cudaId = self.deviceIndex.value % torch.cuda.device_count()
            deviceName = "cuda:" + str(cudaId)
            self.deviceIndex.value = self.deviceIndex.value + 1
        return torch.device(deviceName)

