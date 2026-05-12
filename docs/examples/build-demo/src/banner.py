def box(top: str, bottom: str) -> str:
    width = max(len(top), len(bottom)) + 4
    line = "+" + "-" * (width - 2) + "+"
    return "\n".join(
        [
            line,
            f"| {top.center(width - 4)} |",
            f"| {bottom.center(width - 4)} |",
            line,
        ]
    )
