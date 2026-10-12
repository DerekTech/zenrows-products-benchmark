import sys
seen = []
for line in open('walmart_raw.txt'):
    u = line.strip().split('?')[0]
    if u and u not in seen:
        seen.append(u)
open('walmart_urls.txt', 'w').write('\n'.join(seen) + '\n')
print(len(seen), 'clean URLs')