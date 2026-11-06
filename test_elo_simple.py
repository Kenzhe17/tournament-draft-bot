"""Simple test for ELO calculation logic without imports."""

def get_elo_multiplier(current_elo):
    if current_elo < 1200:
        return 1.5
    elif current_elo < 1400:
        return 1.3
    elif current_elo < 1600:
        return 1.1
    elif current_elo < 1800:
        return 0.9
    else:
        return 0.7

def get_loss_reduction(current_elo):
    if current_elo < 1100:
        return 0.5
    elif current_elo < 1300:
        return 0.7
    else:
        return 1.0

def calculate_personal_bonus(current_kills, current_deaths, avg_kills, avg_deaths):
    if avg_kills <= 0:
        return 0
    kills_improvement = current_kills - avg_kills
    deaths_improvement = avg_deaths - current_deaths
    bonus = (kills_improvement * 2) + deaths_improvement
    return max(0, bonus)

def calculate_catch_up_bonus(current_elo, avg_server_elo):
    if avg_server_elo <= 0:
        return 0
    elo_diff = avg_server_elo - current_elo
    if elo_diff > 300:
        return 5
    elif elo_diff > 200:
        return 3
    elif elo_diff > 100:
        return 1
    return 0

def get_base_elo_change(position, team_won):
    if team_won:
        base_changes = {1: 12, 2: 10, 3: 8, 4: 6}
    else:
        base_changes = {1: -2, 2: -4, 3: -6, 4: -8}
    return base_changes.get(position, 0)

def calculate_kd_bonus(kills, deaths):
    return (kills * 2) - deaths

def calculate_balanced_elo_change(position, team_won, kills, deaths, current_elo=1000, avg_kills=0.0, avg_deaths=0.0, avg_server_elo=0.0):
    base_change = get_base_elo_change(position, team_won)
    kd_bonus = calculate_kd_bonus(kills, deaths)
    total_base = base_change + kd_bonus

    elo_multiplier = get_elo_multiplier(current_elo)
    adjusted_change = int(total_base * elo_multiplier)

    if adjusted_change < 0:
        loss_reduction = get_loss_reduction(current_elo)
        adjusted_change = int(adjusted_change * loss_reduction)

    if avg_kills > 0:
        personal_bonus = calculate_personal_bonus(kills, deaths, avg_kills, avg_deaths)
        adjusted_change += personal_bonus

    catch_up_bonus = calculate_catch_up_bonus(current_elo, avg_server_elo)
    adjusted_change += catch_up_bonus

    return adjusted_change

print("=== Testing ELO Multipliers ===")
test_elos = [1000, 1100, 1300, 1500, 1700, 1900, 2000]
for elo in test_elos:
    print(f"{elo} ELO: {get_elo_multiplier(elo)}x")

print("\n=== Testing Loss Reduction ===")
for elo in [1000, 1100, 1300, 1500]:
    print(f"{elo} ELO: {get_loss_reduction(elo)}x penalty")

print("\n=== Testing Complete Balanced ELO Change ===")
avg_server_elo = 1500

test_cases = [
    {"desc": "Weak player (1000 ELO) wins with 8/4", "pos": 2, "won": True, "k": 8, "d": 4, "elo": 1000, "avg_k": 3, "avg_d": 6},
    {"desc": "Average player (1500 ELO) wins with 6/5", "pos": 2, "won": True, "k": 6, "d": 5, "elo": 1500, "avg_k": 6, "avg_d": 5},
    {"desc": "Top player (2000 ELO) wins with 10/4", "pos": 1, "won": True, "k": 10, "d": 4, "elo": 2000, "avg_k": 10, "avg_d": 4},
    {"desc": "Weak player (1000 ELO) loses with 3/8", "pos": 3, "won": False, "k": 3, "d": 8, "elo": 1000, "avg_k": 3, "avg_d": 6},
    {"desc": "Average player (1500 ELO) loses with 4/7", "pos": 3, "won": False, "k": 4, "d": 7, "elo": 1500, "avg_k": 6, "avg_d": 5},
]

for case in test_cases:
    change = calculate_balanced_elo_change(
        case["pos"], case["won"], case["k"], case["d"],
        case["elo"], case["avg_k"], case["avg_d"], avg_server_elo
    )
    print(f"{case['desc']}: {change:+d} ELO")

print("\n=== All tests completed ===")