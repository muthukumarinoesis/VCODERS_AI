"""A stand-in for Gemini: answers every requested slot with a valid question,
unless told to break some."""

import itertools
import json
import re
import threading

SLOT = re.compile(r"^\d+\. type (\w+), difficulty (\w+)$", re.M)


def make_question(kind, level, n, extra=""):
    q = {"type": kind, "difficulty": level, "explanation": "Because the material says so."}
    text = f"Question {n}{extra}: what does the material say about item {n}?"
    if kind == "mcq_single":
        q.update(question=text, options=[f"A{n}", f"B{n}", f"C{n}", f"D{n}"], answer=1)
    elif kind == "mcq_multi":
        q.update(question=text, options=[f"A{n}", f"B{n}", f"C{n}", f"D{n}"], answers=[0, 2])
    elif kind == "true_false":
        q.update(question=f"Statement {n}{extra} is in the material.", answer=True)
    elif kind == "assertion_reason":
        q.update(assertion=f"Assertion {n}{extra}", reason=f"Reason {n}", answer="A")
    elif kind == "fill_blank":
        q.update(question=f"Item {n}{extra} is made of ____.", answer="cells")
    elif kind == "match":
        q.update(question=f"Match set {n}{extra}.", pairs=[[f"L{n}{i}", f"R{n}{i}"] for i in range(3)])
    return q


class FakeLLM:
    def __init__(self, transcript="", break_first=0, broken=None, fail_calls=0, reply=None, reject=None,
                 verify_fails=0):  # fmt: skip
        self.prompts, self.image_calls, self.verify_prompts = [], [], []
        self.reject = reject or (lambda q: False)  # the checker fails questions this returns True for
        self.verify_fails = verify_fails
        self.transcript = transcript
        self.break_first = break_first  # the first N questions made are invalid
        self.broken = broken or (lambda q: {**q, "options": ["same", "same", "x", "y"]})
        self.fail_calls = fail_calls
        self.reply = reply
        self.counter = itertools.count(1)
        self.lock = threading.Lock()
        self.made = 0

    def complete(self, prompt, images=(), json_=None, model=None, **kw):
        with self.lock:
            if images:
                self.image_calls.append((prompt, list(images), model))
                return self.transcript
            if prompt.startswith("You check quiz questions"):
                self.verify_prompts.append(prompt)
                if self.verify_fails:
                    self.verify_fails -= 1
                    raise ConnectionError("checker unavailable")
                shown = re.findall(r"^(\d+)\. (\{.*\})$", prompt, re.M)
                return json.dumps(
                    {"results": [{"n": int(n), "ok": not self.reject(json.loads(q))} for n, q in shown]}
                )
            self.prompts.append((prompt, model))
            if self.fail_calls:
                self.fail_calls -= 1
                raise ConnectionError("Gemini unavailable")
            if self.reply is not None:
                return self.reply(prompt)
            out = []
            topic = re.search(r"^Topic: (.+)$", prompt, re.M)
            for kind, level in SLOT.findall(prompt):
                n = next(self.counter)
                q = make_question(kind, level, n, f" about {topic.group(1)}" if topic else "")
                self.made += 1
                if self.made <= self.break_first:
                    q = self.broken(q)
                out.append(q)
            return json.dumps({"questions": out})
