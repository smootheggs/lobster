"""Seal Shiba's rules. Run this yourself, on the tablet, after reading any
change to identity.txt, actions.py or shiba.py:

    python seal.py

Sloth Lobster refuses to start if those files change after sealing. Never let
Claude Code, a script or Sloth Lobster run this for you: sealing is your signature.
"""
import shiba

if __name__ == "__main__":
    problem = shiba.seal_problem()
    print(f"Before: {problem or 'seal matches'}")
    print(shiba.write_seal())
