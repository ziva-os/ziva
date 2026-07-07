import multiprocessing
from ziva.app.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
