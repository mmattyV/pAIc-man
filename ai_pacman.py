#!/usr/bin/env python3
"""
AI Pacman Agent Implementation
Provides different difficulty levels of AI pacman for ghost players to chase
"""
import random
import logging
from helpers.game import Actions, Directions
from helpers.util import manhattanDistance, Counter
from helpers.pacmanAgents import GreedyAgent

logger = logging.getLogger("ai-pacman")

class AIPacmanAgent:
    """Base class for AI Pacman agents of different difficulty levels"""
    def __init__(self):
        self.last_action = Directions.STOP

    def get_action(self, game_state):
        """
        Get the action for Pacman given the current game state.
        
        Args:
            game_state: A GameState object containing the current state of the game
                including pacman position, ghost positions, food positions, etc.
        
        Returns:
            A direction from Directions (NORTH, SOUTH, EAST, WEST, or STOP)
        
        Raises:
            NotImplementedError: This is an abstract method that must be implemented by subclasses
        """
        raise NotImplementedError("Subclasses must implement get_action")

class EasyAIPacman(AIPacmanAgent):
    """
    Easy difficulty AI Pacman
    
    Simply moves randomly but avoids walls and tries not to stop
    """
    def get_action(self, game_state):
        """
        Get the action for Easy AI Pacman given the current game state.
        
        This implementation makes random moves, with a slight preference for continuing
        in the same direction if possible (60% probability). It avoids the STOP action
        unless there are no other legal actions available.
        
        Args:
            game_state: A GameState object containing the current state of the game
                including pacman position, ghost positions, food positions, etc.
        
        Returns:
            A direction from Directions (NORTH, SOUTH, EAST, WEST, or STOP)
        """
        # Get the legal actions for pacman excluding STOP
        legal_actions = [action for action in game_state.pacman_legal_actions if action != Directions.STOP]
        
        # If we have legal actions, choose one randomly
        if legal_actions:
            # Slightly prefer continuing in the same direction if possible
            if self.last_action in legal_actions and random.random() < 0.6:
                self.last_action = self.last_action
            else:
                self.last_action = random.choice(legal_actions)
        else:
            # If there are no legal actions besides STOP, then STOP
            self.last_action = Directions.STOP
        
        return self.last_action

class MediumAIPacman(AIPacmanAgent):
    """
    Medium difficulty AI Pacman
    
    Uses a modified greedy approach - tries to eat nearby food but also
    avoids ghosts when they are close.
    """
    def __init__(self):
        """
        Initialize the Medium difficulty AI Pacman agent.
        
        Sets the fear_distance parameter that determines at what distance
        the agent starts avoiding ghosts.
        """
        super().__init__()
        self.fear_distance = 3  # Distance at which pacman becomes afraid of ghosts
    
    def get_action(self, game_state):
        """
        Get the action for Medium AI Pacman given the current game state.
        
        This implementation uses a greedy approach that scores actions based on:
        1. Proximity to food (higher score for actions leading to closer food)
        2. Avoiding non-scared ghosts that are within the fear_distance
        
        Args:
            game_state: A GameState object containing the current state of the game
                including pacman position, ghost positions, food positions, etc.
        
        Returns:
            A direction from Directions (NORTH, SOUTH, EAST, WEST, or STOP)
        """
        # Get the legal actions for pacman excluding STOP
        legal_actions = [action for action in game_state.pacman_legal_actions if action != Directions.STOP]
        
        if not legal_actions:
            self.last_action = Directions.STOP
            return self.last_action
        
        # Score each action based on food proximity and ghost avoidance
        action_scores = Counter()
        
        pacman_pos = game_state.pacman_position
        ghost_positions = game_state.ghost_positions
        ghost_scared_timers = game_state.ghost_scared_timers
        food_positions = game_state.food_positions
        
        # Evaluate each legal action
        for action in legal_actions:
            # Simulate the action
            vector = Actions.directionToVector(action)
            new_pos = (pacman_pos[0] + vector[0], pacman_pos[1] + vector[1])
            
            # Initialize score - prefer food
            min_food_dist = min([manhattanDistance(new_pos, food_pos) for food_pos in food_positions]) if food_positions else 0
            action_scores[action] = 10.0 / (min_food_dist + 1.0) if min_food_dist > 0 else 10.0
            
            # Avoid non-scared ghosts that are close
            for i, ghost_pos in enumerate(ghost_positions):
                if ghost_scared_timers[i] <= 0:  # Ghost is not scared
                    ghost_dist = manhattanDistance(new_pos, ghost_pos)
                    if ghost_dist < self.fear_distance:
                        # Drastically reduce score for actions leading toward ghosts
                        action_scores[action] -= (self.fear_distance - ghost_dist) * 5.0
        
        # Choose the action with the highest score
        if action_scores:
            best_score = max(action_scores.values())
            best_actions = [a for a, s in action_scores.items() if s == best_score]
            self.last_action = random.choice(best_actions)
        else:
            # Fallback to random
            self.last_action = random.choice(legal_actions)
            
        return self.last_action

class HardAIPacman(AIPacmanAgent):
    """
    Hard difficulty AI Pacman
    
    Uses a sophisticated strategy to maximize food collection and avoid ghosts.
    Considers capsules to strategically hunt ghosts when advantageous.
    """
    def __init__(self):
        """
        Initialize the Hard difficulty AI Pacman agent.
        
        Sets various parameters that determine the agent's behavior:
        - fear_distance: Distance at which pacman starts avoiding ghosts (larger than medium)
        - close_ghost_threshold: Distance at which a ghost is considered dangerously close
        - capsule_attraction: Multiplier to prioritize pursuing power capsules
        """
        super().__init__()
        self.fear_distance = 4  # Increased awareness of ghosts
        self.close_ghost_threshold = 2  # Distance at which a ghost is considered "close"
        self.capsule_attraction = 5.0  # Higher value to prioritize capsules
    
    def get_action(self, game_state):
        """
        Get the action for Hard AI Pacman given the current game state.
        
        This advanced implementation scores actions based on multiple factors:
        1. Proximity to food (higher score for actions leading to closer food)
        2. Strategic pursuit of power capsules, especially when ghosts are nearby
        3. Avoiding non-scared ghosts with increased awareness range
        4. Chasing scared ghosts when it's safe to do so
        5. Maintaining smooth movement patterns when no immediate threats/opportunities
        
        Args:
            game_state: A GameState object containing the current state of the game
                including pacman position, ghost positions, food positions, etc.
        
        Returns:
            A direction from Directions (NORTH, SOUTH, EAST, WEST, or STOP)
        """
        # Get the legal actions for pacman excluding STOP
        legal_actions = [action for action in game_state.pacman_legal_actions if action != Directions.STOP]
        
        if not legal_actions:
            self.last_action = Directions.STOP
            return self.last_action
        
        # Score each action based on multiple factors
        action_scores = Counter()
        
        pacman_pos = game_state.pacman_position
        ghost_positions = game_state.ghost_positions
        ghost_scared_timers = game_state.ghost_scared_timers
        food_positions = game_state.food_positions
        capsule_positions = game_state.capsule_positions
        
        # Evaluate each legal action
        for action in legal_actions:
            # Simulate the action
            vector = Actions.directionToVector(action)
            new_pos = (pacman_pos[0] + vector[0], pacman_pos[1] + vector[1])
            
            # Base score based on food proximity
            action_scores[action] = 0
            
            # Food attraction
            if food_positions:
                min_food_dist = min([manhattanDistance(new_pos, food_pos) for food_pos in food_positions])
                action_scores[action] += 15.0 / (min_food_dist + 1.0)
            
            # Capsule attraction - stronger than food
            if capsule_positions:
                min_capsule_dist = min([manhattanDistance(new_pos, cap_pos) for cap_pos in capsule_positions])
                if min_capsule_dist < 5:  # Only prioritize nearby capsules
                    action_scores[action] += self.capsule_attraction * (5.0 / (min_capsule_dist + 1.0))
            
            # Ghost avoidance or chase
            any_close_ghosts = False
            any_scared_ghosts = False
            for i, ghost_pos in enumerate(ghost_positions):
                ghost_dist = manhattanDistance(new_pos, ghost_pos)
                
                if ghost_scared_timers[i] > 0:  # Ghost is scared - chase it!
                    any_scared_ghosts = True
                    # Chase ghosts, but only if they're still scared for a while
                    if ghost_scared_timers[i] > 5:
                        action_scores[action] += max(0, 8.0 - ghost_dist)
                else:  # Ghost is dangerous
                    if ghost_dist < self.fear_distance:
                        any_close_ghosts = True
                        action_scores[action] -= (self.fear_distance - ghost_dist) * 10.0
                        
                        # Severely penalize moves that could lead to being trapped
                        if ghost_dist <= self.close_ghost_threshold:
                            action_scores[action] -= 50.0
            
            # If there are close ghosts and an available capsule, prioritize the capsule
            if any_close_ghosts and capsule_positions:
                min_capsule_dist = min([manhattanDistance(new_pos, cap_pos) for cap_pos in capsule_positions])
                action_scores[action] += 20.0 / (min_capsule_dist + 1.0)
            
            # If there's no immediate danger, consider exploring unexplored areas for more food
            if not any_close_ghosts and not any_scared_ghosts:
                # Slightly favor the current direction for smoother movement patterns
                if action == self.last_action:
                    action_scores[action] += 1.0
                
                # Slightly penalize reversing direction
                reverse = Directions.REVERSE[self.last_action]
                if action == reverse:
                    action_scores[action] -= 2.0
        
        # Choose the action with the highest score
        if action_scores:
            best_score = max(action_scores.values())
            best_actions = [a for a, s in action_scores.items() if s == best_score]
            self.last_action = random.choice(best_actions)
        else:
            # Fallback to random
            self.last_action = random.choice(legal_actions)
            
        return self.last_action

def create_ai_agent(difficulty):
    """
    Factory function to create an AI Pacman agent of the specified difficulty.
    
    Args:
        difficulty (str): The difficulty level for the AI agent.
            Must be one of "EASY", "MEDIUM", or "HARD".
    
    Returns:
        AIPacmanAgent: An instance of the appropriate AI agent class based on
            the specified difficulty. Defaults to MediumAIPacman if an invalid
            difficulty is provided.
    
    Note:
        If an invalid difficulty is specified, a warning will be logged and the
        function will default to returning a MediumAIPacman agent.
    """
    if difficulty == "EASY":
        return EasyAIPacman()
    elif difficulty == "MEDIUM":
        return MediumAIPacman()
    elif difficulty == "HARD":
        return HardAIPacman()
    else:
        logger.warning(f"Unknown difficulty {difficulty}, defaulting to MEDIUM")
        return MediumAIPacman()
