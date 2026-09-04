import urllib.request, urllib.parse, json, sys
from pathlib import Path

def fetch_batch(dataset, split, offset, length):
    query = urllib.parse.urlencode({
        'dataset': dataset,
        'config': 'default',
        'split': split,
        'offset': offset,
        'length': length,
    })
    with urllib.request.urlopen(
        f'https://datasets-server.huggingface.co/rows?{query}',
        timeout=120) as response:
        return json.loads(response.read().decode('utf-8'))

def tests(value):
    if not isinstance(value, str):
        return value
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    return [str(item) for item in parsed] if isinstance(parsed, list) else value

def main():
    dataset = 'princeton-nlp/SWE-bench_Verified'
    split = 'test'
    target = 500
    records = []
    batch_size = 50
    offset = 0
    
    while len(records) < target:
        actual_batch = min(batch_size, target - len(records))
        print(f'Fetching offset={offset}, batch={actual_batch}...', file=sys.stderr)
        try:
            data = fetch_batch(dataset, split, offset, actual_batch)
        except Exception as e:
            print(f'Error at offset {offset}: {e}', file=sys.stderr)
            break
        rows = data.get('rows', [])
        if not rows:
            print(f'No more rows at offset {offset}', file=sys.stderr)
            break
        for item in rows:
            row = item['row']
            records.append({
                'id': row['instance_id'],
                'category': 'real_swebench',
                'repo': row['repo'],
                'base_commit': row['base_commit'],
                'problem_statement': row['problem_statement'],
                'patch': row['patch'],
                'test_patch': row['test_patch'],
                'fail_to_pass': tests(row['FAIL_TO_PASS']),
                'pass_to_pass': tests(row['PASS_TO_PASS']),
                'version': row['version'],
            })
        offset += len(rows)
        print(f'Fetched {len(records)} / {target}', file=sys.stderr)
    
    output = Path('benchmark/swebench-verified-500.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({
        'name': 'swebench-verified-500',
        'source': dataset,
        'split': split,
        'tasks': records[:target],
    }, indent=2), encoding='utf-8')
    print(f'Saved {len(records[:target])} tasks to {output}')

if __name__ == '__main__':
    main()
