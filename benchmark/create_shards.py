import json
from pathlib import Path

data = json.loads(Path('benchmark/swebench-verified-500.json').read_text())
tasks = data['tasks']
print(f'Total tasks: {len(tasks)}')
print(f'First task ID: {tasks[0]["id"]}')
print(f'Last task ID: {tasks[-1]["id"]}')

# Create 50 shards of 10 tasks each
shard_size = 10
num_shards = len(tasks) // shard_size
print(f'Shards to create: {num_shards}')

for i in range(num_shards):
    start = i * shard_size
    end = start + shard_size
    shard = tasks[start:end]
    shard_file = Path(f'benchmark/swebench-500-shard-{i}.json')
    shard_file.write_text(json.dumps({
        'name': f'swebench-500-shard-{i}',
        'source': 'princeton-nlp/SWE-bench_Verified',
        'split': 'test',
        'tasks': shard,
    }, indent=2), encoding='utf-8')
    print(f'Created shard {i}: tasks {start}-{end-1} ({len(shard)} tasks)')

print(f'Created {num_shards} shards with {shard_size} tasks each')
