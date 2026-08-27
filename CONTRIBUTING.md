# GoldScope contribution workflow

GoldScope uses a review-first two-branch workflow.

## Branches

- `main` is the reviewed, production-ready branch. Do not commit directly to it.
- `dev` is the integration branch for all new work.

## Every change

1. Start from the latest `dev` branch.
2. Make one scoped change and update tests and documentation where applicable.
3. Run:

   ```bash
   PYTHONPYCACHEPREFIX=/tmp/goldscope-pycache python3 -m unittest -v
   node --check static/app.js
   ```

4. Commit the change to `dev` with a descriptive message.
5. Push `dev` to GitHub.
6. Open a pull request from `dev` into `main`.
7. Merge only after the owner reviews and approves the pull request.

## Releases

- Update `VERSION` according to Semantic Versioning.
- Update `CHANGELOG.md` in the same pull request.
- Create the annotated version tag from `main` only after the pull request is
  approved and merged.
- Deploy the exact reviewed `main` revision to Liara.
