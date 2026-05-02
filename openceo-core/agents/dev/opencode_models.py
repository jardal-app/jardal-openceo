from __future__ import annotations


def apply_opencode_model_override(command: list[str], model_override: str | None) -> list[str]:
    """Replace or append the OpenCode model flag when a central override is configured."""
    override = str(model_override or "").strip()
    if not override:
        return list(command)

    updated: list[str] = []
    replaced = False
    skip_next = False

    for token in command:
        if skip_next:
            skip_next = False
            continue

        if token in {"-m", "--model"}:
            updated.extend([token, override])
            replaced = True
            skip_next = True
            continue

        if token.startswith("--model="):
            updated.append(f"--model={override}")
            replaced = True
            continue

        if token.startswith("-m="):
            updated.append(f"-m={override}")
            replaced = True
            continue

        updated.append(token)

    if not replaced:
        updated.extend(["-m", override])

    return updated
