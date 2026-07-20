"""Price check from MEASURED costs, not estimates."""
IN_R, OUT_R, READ_R, WRITE_R = 3.00, 15.00, 0.30, 6.00

# All measured live this session:
HOMEWORK_Q   = 0.0069   # screenshot -> answer, stripped path (Sonnet, image in)
CHAT_TURN    = 0.0087   # ordinary conversation turn, cache hit
TEST_6Q      = 0.0817   # generate + verify a 6-question test (AP Biology, measured)
TEST_Q       = TEST_6Q / 6

print("  MEASURED unit costs")
print(f"    homework question (screenshot -> answer) : ${HOMEWORK_Q:.4f}")
print(f"    conversation turn                        : ${CHAT_TURN:.4f}")
print(f"    practice test, 6 questions, keys verified: ${TEST_6Q:.4f}  (${TEST_Q:.4f}/q)")
print()

PROFILES = [
    ("casual      (2 hw/day, 1 test/wk)",      2,  1/7, 1),
    ("normal      (6 hw/day, 3 tests/wk)",     6,  3/7, 3),
    ("heavy       (15 hw/day, 5 tests/wk)",   15,  5/7, 6),
    ("exam week   (30 hw/day, 1 test/day)",   30,  1.0, 10),
]
print(f"  {'profile':<38}{'$/mo':>7}{'@$5':>8}{'@$10':>8}{'@$15':>8}")
print("  " + "-"*69)
for name, hw, tests, chats in PROFILES:
    c = (hw*HOMEWORK_Q + tests*TEST_6Q + chats*CHAT_TURN) * 30
    print(f"  {name:<38}{c:>7.2f}{5-c:>8.2f}{10-c:>8.2f}{15-c:>8.2f}")

print("\n  A realistic mix (most users are casual/normal, a few are heavy):")
mix = [(0.5, 2, 1/7, 1), (0.35, 6, 3/7, 3), (0.15, 15, 5/7, 6)]
blended = sum(w*(hw*HOMEWORK_Q + t*TEST_6Q + c*CHAT_TURN)*30 for w,hw,t,c in mix)
print(f"    blended cost/user/month = ${blended:.2f}")
for p in (5, 10, 15):
    print(f"      at ${p:>2}/mo → margin ${p-blended:>5.2f}/user  ({(p-blended)/p*100:>3.0f}%)")

print("\n  Credit packs (prepaid — no surprise bills, sellable for cash):")
for q, price in ((100, 5), (250, 10), (600, 20)):
    # assume 70% homework questions, 30% practice-test questions
    cost = q*(0.7*HOMEWORK_Q + 0.3*TEST_Q)
    print(f"    {q:>3} questions for ${price:<3} → costs ${cost:>5.2f}, margin ${price-cost:>5.2f} ({(price-cost)/price*100:.0f}%)")

print("\n  What $20 of Anthropic credit buys you:")
print(f"    ~{int(20/blended)} users for a month at the blended rate")
