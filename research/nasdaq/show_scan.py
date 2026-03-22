import csv, sys

path = sys.argv[1] if len(sys.argv) > 1 else "research/data/nasdaq/scan_20200130.csv"
rows = list(csv.DictReader(open(path)))

print(f"{'RANK':>4}  {'SYMBOL':<8}  {'SCORE':>6}  {'EXECS':>8}  {'TRADES':>8}  {'AVG_PRICE':>10}  {'MIN':>7}  {'MAX':>7}")
print("-" * 75)
for r in rows[:50]:
    print(f"{r['rank']:>4}  {r['symbol']:<8}  {float(r['score']):>6.1f}"
          f"  {r['exec_count']:>8}  {r['trade_count']:>8}"
          f"  ${float(r['avg_price']):>9.2f}"
          f"  ${float(r['min_price']):>6.2f}"
          f"  ${float(r['max_price']):>6.2f}")
