"""The mock company ("Northwind Robotics") that the AI employee works inside.

Everything here is the *environment*, not the agent: a legacy finance portal
(web UI only), a mailbox service (REST API), a shared drive, the company
handbook, fault injection ("chaos"), and ground-truth checkers used to grade
runs. The agent never imports this package; it only reaches the world through
the same interfaces a human employee would (browser, mail API, files).
"""
