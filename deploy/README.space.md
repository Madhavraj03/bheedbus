---
title: BheedBus
emoji: 🚌
colorFrom: green
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
short_description: How full is your Delhi bus? Crowding-aware journey planner
---

# BheedBus

A crowding-aware bus journey planner for Delhi, built for the Transport Stack Hackathon 2026 (Problem 2).
It uses 2.56 million October 2026 ticket records to estimate how full each bus usually is, by route, stretch and hour.
Trip options show travel time, crowding and the chance of getting a seat.

- **Trip:** plan a trip, then pick the fastest option or the one with a seat
- **Routes:** a map of every stop, coloured by crowding for any hour
- **Operators:** overloaded routes, and a fleet rebalancer
- **WhatsApp:** text or voice notes with tap-to-choose menus (webhook at `/webhook/whatsapp`)

The estimates are typical values, not live data. The method and its limits are in the technical note under the Data tab.
