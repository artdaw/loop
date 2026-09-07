"""Shipped interfaces over the vNext stack: CLI, HTTP, bot.

Every command/route here calls `loop.app.build_application` and nothing else
constructs its own services — that is what "CLI, bot and web share application
services" (agent-stack §1) actually means in code, not just in the contract.
"""
