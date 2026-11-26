"""D-2 — benign deploy: log-message wording and a README. Changes no behaviour.

The trap: reworded messages are new log templates, so the novelty detector correctly flags
them — and an agent that equates "novel" with "causal" blames this commit.
"""

EDITS = [
    {
        "op": "replace_string",
        "file": "nimbus/gateway/main.py",
        "old": "checkout failed at the orders-svc call",
        "new": "checkout could not be completed: orders-svc call failed",
    },
    {
        "op": "replace_string",
        "file": "nimbus/payments_svc/main.py",
        "old": "payment provider call failed",
        "new": "call to payment provider failed",
    },
    {
        "op": "write_file",
        "file": "README.md",
        "content": "# Nimbus Store\n\nCheckout: gateway -> orders-svc -> payments-svc -> psp.\n"
                   "Fulfillment: worker, polling the jobs table.\n",
    },
]
