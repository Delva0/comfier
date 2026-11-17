"""Run the H3 story with a small computation shipped from this very file.

Arguments and native H3 nodes are in minimax_h3_remote.py. The source file is
loaded on the remote Python; its main block runs only on the client.
"""


def soften(image):
    """Pure remote computation; imports it needs would belong here or at module scope."""
    return image * 0.5


if __name__ == "__main__":
    import argparse
    import asyncio
    from pathlib import Path

    import minimax_h3_remote as h3

    import comfier as cf

    def transform(image):
        return cf.python(source=Path(__file__), entry="soften", inputs={"image": image})

    parser = argparse.ArgumentParser(description=__doc__)
    h3.add_arguments(parser)
    asyncio.run(h3.run(parser.parse_args(), transform=transform))
