"""MMW curriculum data for SkillBridge."""

MODULES = [
    "Nature of Mathematics", "Mathematical Language", "Problem Solving & Reasoning",
    "Data Management", "Central Tendency", "Variability", "Normal Distribution",
    "Correlation & Regression", "Apportionment & Voting", "Graphs",
]

# Set A — pretest
KEY = [
    "BDBCBBCABB", "BBBABCCCBB", "CCABCCBCCA", "BBCCABDBCC", "BBCCBDCCAB",
    "CBCABABACC", "CBBBCCBCAB", "BCCCBBCBBB", "BBBDBCCABC", "BBBBCCBCBB",
]
# Set B — retest (avoids re-using the pretest items)
KEY_B = [
    "BCBBCBBABB", "BBBABCBBCB", "BCBCBCACCC", "BBCCBACBBB", "CBCCBDBCBB",
    "BBCCBBCBDB", "BCBBBBBCAC", "ACBCABCCCB", "BBBCADBABD", "BABBACABBB",
]
# Set C — long test / final boss
KEY_C = [
    "CCABBBAABB", "CBBABCCCBB", "CDBCCDBCDC", "BCCCCBDBCD", "CBCCBDBCCB",
    "CCBCBCCCDB", "BBCBBCBCBD", "BBABCBBCBC", "BBBCCDBBCD", "BBBACCABBB",
]

CORRECTED = {"5.7": "B", "6.4": "B", "9.4": "C", "9.5": "A"}

CATS = [
    "FPCPCCCCCC", "CCCCPPPFCC", "PPCPCPCPPF", "CCCCCCCCCC", "FPFFPCPFCP",
    "FFCFCPFCPP", "CCPPCCPCPC", "CCCCCPPFCF", "PFCPCPCCCP", "CCCCFPPCPC",
]

# (name, module_id, subtopic_index, positions) — subtopic_index maps into SUBTOPICS
SUBTOPICS = [
    (0, "Patterns in Nature"), (0, "The Golden Ratio"),
    (1, "Mathematical Language"), (1, "Sets and Operations"),
    (2, "Inductive and Deductive"), (2, "Problem Solving Strategies"),
    (3, "Data Basics"), (3, "Variable Classifications"),
    (4, "Mean and Median"), (4, "Mode and Quantiles"),
    (5, "Range"), (5, "Variance and SD"),
    (6, "Normal Curve"), (6, "Z-Scores"),
    (7, "Correlation"), (7, "Regression"),
    (8, "Apportionment"), (8, "Voting Methods"), (8, "Weighted Voting"),
    (9, "Graph Structure"), (9, "Planar Graphs"),
]

SKILL_SPECS = [
    ("Number patterns", 0, 0, "1 2 4 6"),
    ("Fibonacci & golden ratio", 0, 1, "5 7 9"),
    ("Nature & role of math", 0, 0, "3 8 10"),
    ("Symbols & propositions", 1, 2, "1 2 9 10"),
    ("Set notation", 1, 3, "3 4 8"),
    ("Set operations", 1, 3, "5 6 7"),
    ("Sequences & induction", 2, 4, "1 2 4 6 9"),
    ("Reasoning & applied", 2, 5, "3 5 7 8 10"),
    ("Population & sample", 3, 6, "1 2 8"),
    ("Variables & data types", 3, 7, "3 4 9"),
    ("Levels of measurement", 3, 7, "5 6 7 10"),
    ("Mean", 4, 8, "1 4 8"),
    ("Median & quartiles", 4, 9, "2 5 7 10"),
    ("Mode & outliers", 4, 9, "3 6 9"),
    ("Range", 5, 10, "1 2 3 4 7 8"),
    ("Std deviation & variance", 5, 11, "5 6 9 10"),
    ("Normal curve properties", 6, 12, "1 2 6"),
    ("z-score computation", 6, 13, "3 4 7 9"),
    ("z meaning & empirical rule", 6, 12, "5 8 10"),
    ("Interpreting r", 7, 14, "1 2 5"),
    ("r range & special values", 7, 14, "3 4 9"),
    ("Regression & prediction", 7, 15, "6 7 8 10"),
    ("Apportionment & plurality", 8, 16, "1 2 3"),
    ("Borda & instant runoff", 8, 17, "4 5 6"),
    ("Weighted voting", 8, 18, "7 8 9 10"),
    ("Graph structure & Euler", 9, 19, "1 2 3 4 5 9"),
    ("Planar graphs & coloring", 9, 20, "6 7 8 10"),
]

EDGES = [(4, 5), (0, 1), (11, 15), (15, 17), (19, 21), (25, 26)]

RESOURCES = [
    ("R-M05-001", "Mean computation (video)", {11: 1.0}),
    ("R-M06-002", "Variance & SD (video)", {15: 1.0, 11: 0.3}),
    ("R-M07-002", "z-scores (video)", {17: 1.0, 15: 0.3, 11: 0.3}),
    ("R-M05-002", "Median & quartiles (practice)", {12: 1.0}),
    ("R-M06-001", "Range (practice)", {14: 1.0}),
    ("R-M01-001", "Fibonacci & golden ratio (video)", {1: 1.0, 0: 0.3}),
    ("R-M02-001", "Set operations (practice)", {5: 1.0, 4: 0.5}),
    ("R-M08-001", "Regression basics (video)", {21: 1.0, 19: 0.5}),
    ("R-M08-002", "Interpreting r (practice)", {19: 1.0}),
    ("R-M10-001", "Euler & degree (video)", {25: 1.0, 26: 0.3}),
    ("R-M09-001", "Weighted voting (video)", {24: 1.0}),
    ("R-M04-002", "Levels of measurement (practice)", {10: 1.0, 9: 0.5}),
]

FOCUS = [
    "find the rule of a number pattern and apply it to the next term",
    "each term is the sum of the two before it; recall the golden ratio",
    "recognise patterns in nature and how mathematics describes them",
    "translate words into algebra and identify true statements",
    "read and write sets with braces and set-builder notation",
    "find intersections, unions, and subsets of sets",
    "predict the next term from the pattern in the differences or ratios",
    "use the four Polya steps and solve word problems step by step",
    "tell a population from a sample and identify the sample size",
    "classify variables as qualitative or quantitative, discrete or continuous",
    "nominal, ordinal, interval, or ratio: check for order and a true zero",
    "add all values and divide by the count",
    "sort the data first, then find the middle value or quartile",
    "find the most frequent value and see how extremes pull the mean",
    "subtract the smallest value from the largest",
    "square the deviations, average them, then take the square root",
    "the normal curve is bell-shaped and symmetric about the mean",
    "z equals (x minus the mean) divided by the standard deviation",
    "about 68, 95, and 99.7 percent of data lie within 1, 2, and 3 standard deviations",
    "read the sign and size of the correlation coefficient",
    "r lies between -1 and 1; r = 0 means no linear relationship",
    "use the slope and intercept to predict y from x",
    "use the standard divisor and quota; plurality means the most votes",
    "Borda gives n-1 points to a first choice and 0 to a last choice",
    "a coalition wins when its weights reach the quota",
    "degree counts the edges at a vertex; 0 odd vertices give a circuit, 2 give a path",
    "Euler's formula v - e + f = 2; planar graphs need at most 4 colours",
]


def item_id(i, set_code="A"):
    """Position 0-99 to item ID. Set A has no prefix; B and C add one."""
    base = f"{i // 10 + 1}.{i % 10 + 1}"
    return base if set_code == "A" else set_code + base


def item_index(iid):
    """Item ID to position 0-99. Ignores a leading B/C set prefix."""
    if iid and iid[0] in "BC":
        iid = iid[1:]
    m, p = iid.split(".")
    return (int(m) - 1) * 10 + int(p) - 1


def key_letters(corrected=False):
    letters = [c for row in KEY for c in row]
    if corrected:
        for iid, letter in CORRECTED.items():
            letters[item_index(iid)] = letter
    return letters


def set_key(set_code="A", corrected=False):
    if set_code == "B":
        return [c for row in KEY_B for c in row]
    if set_code == "C":
        return [c for row in KEY_C for c in row]
    return key_letters(corrected)


def categories():
    return [c for row in CATS for c in row]


def skills():
    """Returns [(name, module index, subtopic index, [global item indices])]."""
    return [(name, mod, sub, [mod * 10 + int(p) - 1 for p in pos.split()])
            for name, mod, sub, pos in SKILL_SPECS]


def skill_code(k):
    _, mod, _, _ = SKILL_SPECS[k]
    pos = sum(1 for kk, (_, m, _, _) in enumerate(SKILL_SPECS) if m == mod and kk <= k)
    return f"{mod + 1}.{pos}"