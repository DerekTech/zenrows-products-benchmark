seen = []
for line in open('leboncoin_raw.txt'):
    u = line.strip().split('?')[0]
    if u and u not in seen:
        seen.append(u)
open('leboncoin_urls.txt', 'w').write('\n'.join(seen) + '\n')
print(len(seen), 'clean URLs')