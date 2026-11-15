import json, os

total_claims = 0
total_sent = 0
count = 0

for f in os.listdir('output/articles'):
    if f.endswith('.log.json'):
        with open(f'output/articles/{f}') as fp:
            log = json.load(fp)
            kept = log.get('kept', 0)
            sent = log.get('sentences_sent', 0)
            dropped = log.get('dropped', {})
            total_claims += kept
            total_sent += sent
            count += 1
            print(f'{f[:-9]}: claims={kept}, sent={sent}, dropped={dropped}')

print(f'TOTAL: {total_claims} claims, {total_sent} sentences, avg={total_claims/count:.1f} claims/article')