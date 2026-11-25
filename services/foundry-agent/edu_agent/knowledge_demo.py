from edu_domain.contracts import (
    DiscoverKnowledge,
    EnglishFeedback,
    GenerateKnowledge,
    KnowledgeDiscoveryResult,
    KnowledgeFeedbackResult,
    KnowledgePracticeResult,
    ReferenceExercise,
    ReviewKnowledge,
)


def knowledge_demo(request: DiscoverKnowledge | GenerateKnowledge | ReviewKnowledge):
    from edu_agent.demo import DEMO_VERSION, DemoImageOnly, _encoded_sample, demo_analysis

    if isinstance(request, DiscoverKnowledge):
        if request.subject == "english":
            from base64 import b64encode

            from edu_agent.english_demo import english_demo, english_demo_page
            from edu_domain.contracts import AnalyzeEnglish
            from edu_domain.images import normalize_page

            if request.image_base64 != b64encode(normalize_page(english_demo_page())).decode("ascii"):
                raise DemoImageOnly
            analyzed = english_demo(AnalyzeEnglish(
                version="1", operation="analyze_english", image_base64=request.image_base64,
            ))
            points = {point.id: point for point in request.catalog}
            items = []
            for item in analyzed.items:
                point = points[item.source.prompt.skill]
                items.append({
                    "label": item.source.label, "region": item.source.region,
                    "stem": item.source.prompt.instruction,
                    "points": [{"existing_id": point.id, "title": point.title,
                                "unit": point.unit, "description": "这是合成样例中的已有知识点。"}],
                    "feedback": None,
                })
            return KnowledgeDiscoveryResult(
                version="1", operation=request.operation, quality="readable", items=items, provenance=DEMO_VERSION,
            )
        if request.image_base64 != _encoded_sample():
            raise DemoImageOnly
        from edu_domain.math import verify

        points = {point.id: point for point in request.catalog}
        items = []
        for analyzed in demo_analysis().items:
            point = points[verify(analyzed.spec).skill]
            items.append({
                "label": analyzed.source.label, "region": analyzed.source.region,
                "stem": analyzed.source.stem,
                "points": [{"existing_id": point.id, "title": point.title,
                            "unit": point.unit, "description": "这是合成样例中的已有知识点。"}],
                "feedback": None,
            })
        return KnowledgeDiscoveryResult(
            version="1", operation=request.operation, quality="readable", items=items, provenance=DEMO_VERSION,
        )
    if isinstance(request, GenerateKnowledge):
        instructions = (
            "用自己的话解释这个知识点。", "给出一个使用这个知识点的例子。",
            "描述一个容易混淆的地方。", "写下你会怎样检查自己的答案。", "用一个问题检验你的理解。",
        )
        return KnowledgePracticeResult(
            version="1", operation=request.operation, provenance=DEMO_VERSION,
            items=[ReferenceExercise(
                instruction=instructions[index], context=request.point.title,
                reference_answer="合成演示不提供真实判分，请围绕知识点表达你的理解。",
                explanation="这是本地合成示例；真实参考答案需要连接 Agent。",
            ) for index in range(request.count)],
        )
    return KnowledgeFeedbackResult(
        version="1", operation=request.operation, provenance=DEMO_VERSION,
        feedback=EnglishFeedback(
            strengths=["你完成了这次练习。"], improvements=["这是合成反馈，不能判断你的回答是否正确。"],
            suggested_response="连接真实 Agent 后可获得针对回答的参考建议。",
        ),
    )
