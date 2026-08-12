# Contributing

We welcome contributions to GitHub Repository Insights!

## Local Setup

1. Clone the repository and navigate to the project directory.
2. The recommended way to run the stack is via Docker:
   ```bash
   docker-compose up -d --build
   ```
3. To develop the backend locally without Docker:
   ```bash
   cd backend
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

## Branch Naming

Please use descriptive branch names prefixed with the type of change:
- `feature/description`
- `bugfix/description`
- `docs/description`

## Running Tests

Before opening a pull request, ensure all tests pass:

```bash
cd backend
python -m pytest
```

Ensure your code is properly formatted and adheres to PEP 8 standards. CI will automatically run tests and block PRs if they fail.
