import urllib.request, urllib.parse, json
query = urllib.parse.urlencode({
    'dataset': 'princeton-nlp/SWE-bench_Verified',
    'config': 'default',
    'split': 'test',
    'offset': 0,
    'length': 1,
})
response = urllib.request.urlopen(
    f'https://datasets-server.huggingface.co/rows?{query}',
    timeout=60)
data = json.loads(response.read())
print(f'Total rows: {data.get("num_rows_total", "unknown")}')
print(f'Rows returned: {len(data.get("rows", []))}')
