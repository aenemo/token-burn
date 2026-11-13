import os

# No test may start the real claude program (the plan keep-alive); tests of it switch it on themselves.
os.environ["TOKEN_BURN_CLAUDE_KEEPALIVE"] = "0"
