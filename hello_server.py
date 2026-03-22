from mcp.server.fastmcp import FastMCP

# 创建一个 MCP 服务实例
mcp = FastMCP("MyPythonTool")


# 使用装饰器定义一个工具
@mcp.tool()
def execute_my_code(name: str = "World") -> str:
    """
    这里写工具的描述：当你需要执行特定的 Python 逻辑并打招呼时调用。
    """
    # 这里放你原本的 Python 逻辑
    result = f"Hello, {name}! Your Python code executed successfully."
    return result


if __name__ == "__main__":
    mcp.run()
