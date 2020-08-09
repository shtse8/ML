import random
import numpy as np
import collections
from memories.Transition import Transition

class SimpleMemory(object):
    def __init__(self, capacity: int) -> None:
        self.memory = collections.deque(maxlen=capacity)
        self.capacity = capacity
        self.current = 0

    def add(self, transition: Transition) -> None:
        self.memory.append(transition)

    def get(self, size = 0):
        states = []
        actions = []
        rewards = []
        nextStates = []
        dones = []
        for t in self.memory:
            states.append(t.state)
            actions.append(t.action)
            rewards.append(t.reward)
            nextStates.append(t.nextState)
            dones.append(t.done)
        return (
            np.array(states), 
            np.array(actions).astype(float), 
            np.array(rewards).astype(float), 
            np.array(nextStates), 
            np.array(dones)
        )
    
    def clear(self) -> None:
        self.memory.clear()
        
    def __iter__(self):
        return self

    def __next__(self):
        self.current += 1
        if self.current < len(self.memory):
            return self.memory[self.current]
        self.current = 0
        raise StopIteration