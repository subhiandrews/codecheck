"""`memory embed [--rebuild]`: compute or refresh the local vectors that semantic retrieval ranks against."""

COMMANDS = ("embed",)
BUDGET_SECONDS = 120.0


def add_arguments(sub) -> None:
    e = sub.add_parser("embed", help="embed the stored evidence text with memory.embed_model (local; needs Ollama)")
    e.add_argument("--rebuild", action="store_true", help="drop every local vector first, then embed again")


def run(args, ctx) -> dict:
    from codecheck.memory import semantic

    return semantic.refresh_local(ctx, rebuild=args.rebuild, budget=BUDGET_SECONDS)
