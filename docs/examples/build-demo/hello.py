import os

from src import banner


def main() -> None:
    print(banner.box(os.environ.get("APP_GREETING", "hi"), "contree build"))


if __name__ == "__main__":
    main()
