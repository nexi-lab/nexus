def hello_world():
    """Say hello to the world."""
    print("Hello, world!")
    return "hello"


def greet(name: str) -> str:
    """Greet a specific person."""
    return f"Hello, {name}!"


class Greeter:
    def __init__(self, greeting: str = "Hello"):
        self.greeting = greeting

    def greet(self, name: str) -> str:
        return f"{self.greeting}, {name}!"
