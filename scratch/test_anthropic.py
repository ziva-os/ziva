import re
content='<think>foo</think>bar'
thinking = re.findall(r'<think[^>]*>([\s\S]*?)</think[^>]*>', content)
main = re.sub(r'<think[^>]*>[\s\S]*?</think[^>]*>', '', content).strip()
print(thinking)
print(main)
