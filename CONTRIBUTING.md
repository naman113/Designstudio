# Contributing

Thanks for helping. Bug reports, fixes and new examples are all welcome.

## Reporting a problem

Open an issue with the bug report form. If you can, attach the STEP file that shows the problem (only files you are
allowed to share) and say what you expected and what you got.

Security problems: do not open a public issue. See [SECURITY.md](SECURITY.md).

## Making a change

1. Fork the repository and clone your fork.
2. Create a branch from `main`: `git switch -c fix/short-name`.
3. Make the change. Keep it focused on one thing.
4. Run the tests (Docker with Compose required):

   ```sh
   docker compose build
   docker compose run --rm --no-deps --entrypoint python cli -m pytest -m "not stack"
   docker compose up -d
   docker compose run --rm --no-deps -e BC_REQUIRE_STACK=1 --entrypoint python cli -m pytest -m stack
   docker compose down
   ```

5. Push the branch to your fork and open a pull request against `main`.

The `main` branch is protected: every change goes through a pull request, the `test` check must pass, and pull
requests are squash-merged. CI on pull requests from forks starts after a maintainer approves it.

## Guidelines

- Every number printed on a drawing must come from the geometry. Do not add hard-coded dimensions.
- Add or update a test for behaviour you change.
- Do not commit customer or confidential CAD files. Test parts should be generated from code (see the samples).

## Licence

By contributing, you agree that your contribution is licensed under the [MIT License](LICENSE), the same licence as
the project.
