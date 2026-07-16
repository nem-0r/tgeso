#!/usr/bin/env python3
"""Run the network-free end-to-end funnel demo (virtual time)."""
import asyncio
from bot.simulate import main

if __name__ == "__main__":
    asyncio.run(main())
