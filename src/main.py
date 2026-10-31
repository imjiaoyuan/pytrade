import multiprocessing

from pytrade.app import main


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
