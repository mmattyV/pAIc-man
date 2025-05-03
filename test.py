import pacman
# Approximate Q Learning Test

pacmanParams = "-p PacmanQAgent -x 2000 -n 2100 -l smallGrid -q -f --fixRandomSeed"
games = pacman.runGames(** pacman.readCommand(pacmanParams.split(' ')))
wins = [g.state.isWin() for g in games].count(True)
assert(wins >= 70)
print("All Q Learning Tests Passed!")

# Approximate Q Learning Test

pacmanParams = "-p ApproximateQAgent -a extractor=SimpleExtractor -x 100 -n 200 -q -l mediumClassic"
games = pacman.runGames(** pacman.readCommand(pacmanParams.split(' ')))
medAvgScore = sum([g.state.getScore() for g in games]) / float(100)
wins = [g.state.isWin() for g in games].count(True)
assert(wins >= 70)
print("All Approximate Q Learning Tests Passed!")

# Custom Feature Extractors Test

pacmanParams = "-p ApproximateQAgent -a extractor=CustomExtractor -x 100 -n 200 -q -l mediumClassic"
games = pacman.runGames(** pacman.readCommand(pacmanParams.split(' ')))
medAvgScoreCustom = sum([g.state.getScore() for g in games]) / float(100)
print(f"SimpleExtractor Average Score: {medAvgScore}, CustomExtractor Average Score: {medAvgScoreCustom}")
wins = [g.state.isWin() for g in games].count(True)
assert(wins >= 70)
assert(medAvgScoreCustom > medAvgScore + 200)

print("All Custom Feature Tests Passed!")