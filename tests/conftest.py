import os
import sys

# pytest imports the tests as package `tests.*`, so plain `import helpers`
# fails without the tests dir on sys.path (unittest discover does this itself).
sys.path.insert(0, os.path.dirname(__file__))
