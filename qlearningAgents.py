# qlearningAgents.py
# ------------------
# Licensing Information:  You are free to use or extend these projects for
# educational purposes provided that (1) you do not distribute or publish
# solutions, (2) you retain this notice, and (3) you provide clear
# attribution to UC Berkeley, including a link to http://ai.berkeley.edu.
#
# Attribution Information: The Pacman AI projects were developed at UC Berkeley.
# The core projects and autograders were primarily created by John DeNero
# (denero@cs.berkeley.edu) and Dan Klein (klein@cs.berkeley.edu).
# Student side autograding was added by Brad Miller, Nick Hay, and
# Pieter Abbeel (pabbeel@cs.berkeley.edu).


from helpers.game import *
from learningAgents import ReinforcementAgent
from featureExtractors import *
from helpers.backend import ReplayMemory

import helpers.backend as backend

import random,util,math
import numpy as np
import copy

class QLearningAgent(ReinforcementAgent):
    """
      Q-Learning Agent
      Functions you should fill in:
        - computeValueFromQValues
        - computeActionFromQValues
        - getQValue
        - getAction
        - update
      Instance variables you have access to
        - self.epsilon (exploration prob)
        - self.alpha (learning rate)
        - self.discount (discount rate)
      Functions you should use
        - self.getLegalActions(state)
          which returns legal actions for a state
    """
    def __init__(self, **args):
        "Initialize Q-values"
        ReinforcementAgent.__init__(self, **args)

        self.values = {}

    def getQValue(self, state, action):
        """
          Returns Q(state,action)
          Should return 0.0 if we have never seen a state
          or the Q node value otherwise
        """
        "*** YOUR CODE HERE ***"
        return self.values.get((state, action), 0.0)

    def computeValueFromQValues(self, state):
        """
          Returns max_action Q(state,action)
          where the max is over legal actions.  Note that if
          there are no legal actions, which is the case at the
          terminal state, you should return a value of 0.0.
        """
        # Get legal actions for the state
        legalActions = self.getLegalActions(state)
        
        # If there are no legal actions, return 0.0
        if not legalActions:
            return 0.0
        
        # Return the maximum Q-value over all legal actions
        return max([self.getQValue(state, action) for action in legalActions])

    def computeActionFromQValues(self, state):
        """
          Compute the best action to take in a state.  Note that if there
          are no legal actions, which is the case at the terminal state,
          you should return None.
        """
        # Get legal actions for the state
        legalActions = self.getLegalActions(state)
        
        # If there are no legal actions, return None
        if not legalActions:
            return None
        
        # Find the maximum Q-value
        maxQValue = self.computeValueFromQValues(state)
        
        # Get all actions that achieve the maximum Q-value
        bestActions = [action for action in legalActions if self.getQValue(state, action) == maxQValue]
        
        # Break ties randomly for better behavior
        return random.choice(bestActions)

    def getAction(self, state):
        """
          Compute the action to take in the current state.  With
          probability self.epsilon, we should take a random action and
          take the best policy action otherwise.  Note that if there are
          no legal actions, which is the case at the terminal state, you
          should choose None as the action.
          HINT: You might want to use util.flipCoin(prob)
          HINT: To pick randomly from a list, use random.choice(list)
        """
        # Pick Action
        legalActions = self.getLegalActions(state)
        action = None
        # Get legal actions for the state
        legalActions = self.getLegalActions(state)
        
        # If there are no legal actions, return None
        if not legalActions:
            return None
        
        # Epsilon-greedy policy: with probability epsilon, choose a random action,
        # otherwise choose the best action according to the current policy
        if util.flipCoin(self.epsilon):
            # Choose a random action with probability epsilon
            return random.choice(legalActions)
        else:
            # Choose the best action according to the current policy
            return self.computeActionFromQValues(state)

    def update(self, state, action, nextState, reward: float):
        """
          The parent class calls this to observe a
          state = action => nextState and reward transition.
          You should do your Q-Value update here
          NOTE: You should never call this function,
          it will be called on your behalf
        """
        # Get the current Q-value for the state-action pair
        currentQValue = self.getQValue(state, action)
        
        # Compute the maximum Q-value for the next state
        maxQNextState = self.computeValueFromQValues(nextState)
        
        # Update the Q-value using the Q-learning formula
        self.values[(state, action)] = (1 - self.alpha) * currentQValue + \
                                      self.alpha * (reward + self.discount * maxQNextState)

    def getPolicy(self, state):
        return self.computeActionFromQValues(state)

    def getValue(self, state):
        return self.computeValueFromQValues(state)


class PacmanQAgent(QLearningAgent):
    "Exactly the same as QLearningAgent, but with different default parameters"

    def __init__(self, epsilon=0.05,gamma=0.8,alpha=0.2, numTraining=0, **args):
        """
        These default parameters can be changed from the pacman.py command line.
        For example, to change the exploration rate, try:
            python pacman.py -p PacmanQLearningAgent -a epsilon=0.1
        alpha    - learning rate
        epsilon  - exploration rate
        gamma    - discount factor
        numTraining - number of training episodes, i.e. no learning after these many episodes
        """
        args['epsilon'] = epsilon
        args['gamma'] = gamma
        args['alpha'] = alpha
        args['numTraining'] = numTraining
        self.index = 0  # This is always Pacman
        QLearningAgent.__init__(self, **args)

    def getAction(self, state):
        """
        Simply calls the getAction method of QLearningAgent and then
        informs parent of action for Pacman.  Do not change or remove this
        method.
        """
        action = QLearningAgent.getAction(self,state)
        self.doAction(state,action)
        return action

class ApproximateQAgent(PacmanQAgent):
    """
       ApproximateQLearningAgent
       You should only have to overwrite getQValue
       and update.  All other QLearningAgent functions
       should work as is.
    """
    def __init__(self, extractor='IdentityExtractor', **args):
        self.featExtractor = util.lookup(extractor, globals())()
        PacmanQAgent.__init__(self, **args)
        self.weights = util.Counter()

    def getWeights(self):
        return self.weights

    def getQValue(self, state, action):
        """
          Should return Q(state,action) = w * featureVector
          where * is the dotProduct operator
        """
        # Initialize Q-value to 0
        qValue = 0.0
        
        # Get the feature vector for this state-action pair
        featureVector = self.featExtractor.getFeatures(state, action)
        
        # Calculate Q(state, action) = w * featureVector
        for feature, value in featureVector.items():
            qValue += self.weights[feature] * value
        
        return qValue

    def update(self, state, action, nextState, reward: float):
        """
           Should update your weights based on transition
        """
        # Get the current Q-value
        currentQValue = self.getQValue(state, action)
        
        # Calculate the maximum Q-value for the next state
        maxQNextState = self.computeValueFromQValues(nextState)
        
        # Calculate the temporal difference (difference between target and current Q-value)
        difference = (reward + self.discount * maxQNextState) - currentQValue
        
        # Get the feature vector for this state-action pair
        featureVector = self.featExtractor.getFeatures(state, action)
        
        # Update each weight based on the formula:
        # w_i ← w_i + α * difference * f_i(s, a)
        for feature, value in featureVector.items():
            self.weights[feature] = self.weights[feature] + self.alpha * difference * value

    def final(self, state):
        """Called at the end of each game."""
        # call the super-class final method
        PacmanQAgent.final(self, state)

        # did we finish training?
        if self.episodesSoFar == self.numTraining:
            # Print the final weights
            print("Final weights:", self.weights)
