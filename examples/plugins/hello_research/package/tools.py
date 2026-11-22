def greet(ctx, arguments):
    from .text.messages import greeting

    return {"greeting": greeting(arguments["name"])}
