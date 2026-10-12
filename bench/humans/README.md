# Scripted colleagues

`--human auto --answers bench/humans/<scenario>.json` answers Nova's questions and approval requests the way a
sensible colleague at Northwind would in that scenario: `answers` match questions by pattern, `reject` turns down
approval requests whose action and details match (the reason Nova gives is not matched: it may quote the wrong
value to explain why it wasn't used; anything else is approved), `default_answer` replaces "I have no more information.
Follow the handbook." A scenario without a file gets those defaults.
