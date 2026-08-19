FROM python:3.12-slim
WORKDIR /workspace
ENTRYPOINT ["python", "-m", "unittest", "-q", "test_solution.py"]
