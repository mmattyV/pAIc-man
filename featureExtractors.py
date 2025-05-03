# featureExtractors.py
# --------------------
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

"Feature extractors for Pacman game states"

from helpers.game import Directions, Actions
import util

class FeatureExtractor:
    def getFeatures(self, state, action):
        """
          Returns a dict from features to counts
          Usually, the count will just be 1.0 for
          indicator functions.
        """
        util.raiseNotDefined()

class IdentityExtractor(FeatureExtractor):
    def getFeatures(self, state, action):
        feats = util.Counter()
        feats[(state,action)] = 1.0
        return feats

class CoordinateExtractor(FeatureExtractor):
    def getFeatures(self, state, action):
        feats = util.Counter()
        feats[state] = 1.0
        feats['x=%d' % state[0]] = 1.0
        feats['y=%d' % state[0]] = 1.0
        feats['action=%s' % action] = 1.0
        return feats

def closestFood(pos, food, walls):
    """
    closestFood -- this is similar to the function that we have
    worked on in the search project; here its all in one place
    """
    fringe = [(pos[0], pos[1], 0)]
    expanded = set()
    while fringe:
        pos_x, pos_y, dist = fringe.pop(0)
        if (pos_x, pos_y) in expanded:
            continue
        expanded.add((pos_x, pos_y))
        # if we find a food at this location then exit
        if food[pos_x][pos_y]:
            return dist
        # otherwise spread out from the location to its neighbours
        actions = [Directions.NORTH, Directions.EAST, Directions.SOUTH, Directions.WEST]
        for a in actions:
            dx, dy = Actions.directionToVector(a)
            nbr_x, nbr_y = int(pos_x + dx), int(pos_y + dy)
            # skip if neighbour is a wall
            if walls[nbr_x][nbr_y]:
                continue
            fringe.append((nbr_x, nbr_y, dist+1))
    # no food found
    return None

def mazeDistance(pos1, pos2, walls):
    """
    Calculates the maze distance between two points using BFS.
    pos1: Starting position (x, y)
    pos2: Target position (x, y)
    walls: Grid of wall locations (boolean)
    Returns the shortest distance or float('inf') if no path exists.
    """
    if pos1 == pos2: return 0
    fringe = util.Queue()
    fringe.push((pos1, 0)) # (position, distance)
    visited = {pos1}

    while not fringe.isEmpty():
        current_pos, dist = fringe.pop()

        if current_pos == pos2:
            return dist

        for action in [Directions.NORTH, Directions.SOUTH, Directions.EAST, Directions.WEST]:
            dx, dy = Actions.directionToVector(action)
            next_x, next_y = int(current_pos[0] + dx), int(current_pos[1] + dy)

            # Check bounds and walls
            if 0 <= next_x < walls.width and 0 <= next_y < walls.height and \
               not walls[next_x][next_y] and (next_x, next_y) not in visited:
                visited.add((next_x, next_y))
                fringe.push(((next_x, next_y), dist + 1))

    return float('inf') # Return infinity if no path found


class SimpleExtractor(FeatureExtractor):
    """
    Returns simple features for a basic reflex Pacman:
    - whether food will be eaten
    - how far away the next food is
    - whether a ghost collision is imminent
    - whether a ghost is one step away
    """

    def getFeatures(self, state, action):
        # extract the grid of food and wall locations and get the ghost locations
        food = state.getFood()
        walls = state.getWalls()
        ghosts = state.getGhostPositions()

        features = util.Counter() # Extension of the dictionary class, where all keys are defaulted to have value 0

        features["bias"] = 1.0

        # compute the location of pacman after he takes the action
        x, y = state.getPacmanPosition()
        dx, dy = Actions.directionToVector(action)
        next_x, next_y = int(x + dx), int(y + dy)

        # count the number of ghosts 1-step away
        features["#-of-ghosts-1-step-away"] = sum((next_x, next_y) in Actions.getLegalNeighbors(g, walls) for g in ghosts)

        # if there is no danger of ghosts then add the food feature
        if not features["#-of-ghosts-1-step-away"] and food[next_x][next_y]:
            features["eats-food"] = 1.0

        dist = closestFood((next_x, next_y), food, walls)
        if dist is not None:
            # make the distance a number less than one otherwise the update
            # will diverge wildly
            features["closest-food"] = float(dist) / (walls.width * walls.height)
        features.divideAll(10.0)
        return features

class CustomExtractor(FeatureExtractor):
    """
    Refined custom feature extractor. Addresses Pacman stopping issues by:
    - Increasing penalty for stopping.
    - Explicitly encouraging movement when ghosts are scared.
    - Making features for eating/chasing scared ghosts more impactful.
    - Using robust inverse distance calculations.
    """

    def getFeatures(self, state, action):
        # Extract game state information
        food = state.getFood()
        walls = state.getWalls()
        capsules = state.getCapsules()
        pacmanPos = state.getPacmanPosition()
        ghostStates = state.getGhostStates()
        # legalActions = state.getLegalActions() # Not needed directly unless checking something specific

        features = util.Counter()

        # --- Basic Features ---
        features["bias"] = 1.0

        # Get the position Pacman will be in after the action
        dx, dy = Actions.directionToVector(action)
        next_x, next_y = int(pacmanPos[0] + dx), int(pacmanPos[1] + dy)
        nextPos = (next_x, next_y)

        # --- Action-Related Features ---
        if action == Directions.STOP:
            # Increase penalty significantly
            features["stop-action"] = 10.0 # Penalize stopping heavily

        # --- Food Features ---
        if food[next_x][next_y]:
            features["eats-food"] = 1.0 # Reward eating food

        distFood = closestFood(nextPos, food, walls)
        if distFood is not None:
             # Use inverse distance + 1: handles dist=0, smoother gradient
            features["inv-dist-food"] = 1.0 / (float(distFood) + 1.0)
        # No else needed, Counter defaults to 0

        # --- Ghost Features ---
        activeGhosts = []
        scaredGhostsInfo = [] # Store (position, timer)
        anyGhostsScared = False

        for ghost in ghostStates:
            if ghost.scaredTimer > 1: # Use > 1 as a buffer? Or just > 0? Let's stick with > 0 for now.
                scaredGhostsInfo.append((ghost.getPosition(), ghost.scaredTimer))
                anyGhostsScared = True
            else:
                activeGhosts.append(ghost.getPosition())

        # Active Ghosts
        activeGhostDists = [mazeDistance(nextPos, ghostPos, walls) for ghostPos in activeGhosts]
        minActiveDist = min(activeGhostDists) if activeGhostDists else float('inf')

        # Penalize proximity to active ghosts heavily
        if minActiveDist <= 1:
             features["active-ghost-1-step-away"] = 2.0 # Increased penalty
        elif minActiveDist <= 3: # Slightly larger radius for caution
              # Inverse distance: Stronger penalty closer
             features["inv-dist-active-ghost-nearby"] = 1.0 / (float(minActiveDist) + 1.0)

        # Scared Ghosts
        reachableScaredGhosts = [] # Store distances to reachable scared ghosts
        for ghostPos, timer in scaredGhostsInfo:
             dist = mazeDistance(nextPos, ghostPos, walls)
             # Check if ghost is reachable before timer expires (add a small buffer maybe?)
             # Allow chasing even if timer is slightly less than dist? Risky. Stick to timer > dist.
             if dist != float('inf') and timer > dist:
                 reachableScaredGhosts.append(dist)

        minScaredDist = min(reachableScaredGhosts) if reachableScaredGhosts else float('inf')

        if minScaredDist == 0: # Pacman is on the same square as a scared ghost
            # Significantly reward eating scared ghost
            features["eats-scared-ghost"] = 5.0
        elif minScaredDist <= 1:
             # Reward being next to a scared ghost
             features["scared-ghost-1-step-away"] = 2.0
        elif minScaredDist != float('inf'):
             # Encourage getting closer to scared ghosts
             features["inv-closest-scared-ghost"] = 1.0 / (float(minScaredDist) + 1.0)

        # Feature to encourage general movement when hunting is possible
        if anyGhostsScared and action != Directions.STOP and minActiveDist > 1: # Don't encourage movement if danger is imminent
             features["hunting-mode"] = 1.0


        # --- Capsule Features ---
        capsuleDists = [mazeDistance(nextPos, capPos, walls) for capPos in capsules]
        minCapsuleDist = min(capsuleDists) if capsuleDists else float('inf')

        if nextPos in capsules:
             features["eats-capsule"] = 1.0 # Reward eating capsule

        if minCapsuleDist != float('inf'):
             features["inv-dist-capsule"] = 1.0 / (float(minCapsuleDist) + 1.0)


        # --- Normalize Features ---
        # Keep normalization consistent
        features.divideAll(10.0)

        return features
