## a16z Show｜Dan Boneh｜AI 能否破解现代密码学，关键在于公开验证而非预先恐慌
节目：Can AI Break Cryptography? What the Experts Actually Think｜Can AI Break Cryptography? What the Experts Actually Think  
链接：https://a16z.simplecast.com/episodes/can-ai-break-cryptography-what-the-experts-actually-think-LCUGnMEC  
时长：00:42:11  

简介：Stanford 教授 Dan Boneh 与 a16z 团队讨论 OpenAI 发布大量数学和计算机科学成果后，市场对 AI 可能迅速破解椭圆曲线密码学的担忧。讨论的重点不是断言风险不存在，而是厘清目前没有针对已部署密码系统的直接突破证据，以及 AI 更可能如何改变密码系统的验证、迁移和软件安全流程。

要点总结：  
1. 现阶段没有证据显示 AI 已削弱任何主流密码假设。Boneh 指出，OpenAI 公布的约 700 项数学与计算机科学进展中，没有密码学问题；外界的恐慌来自 AI 数学能力的间接提升，而不是椭圆曲线或 ECDSA 已被攻破的新信息。  

2. 密码学原本就建立在“某些问题难以高效求解”的假设上，而非严格证明。椭圆曲线离散对数、lattice 的 shortest vector 等问题历经多年攻击仍未被有效攻破，失败的攻击本身会增加信心。Thaler 强调，主流信念并非“算法存在但人类还不够聪明”，而是高效算法可能根本不存在。  

3. AI 若被用于集中测试密码假设，反而可能大幅压缩安全验证周期。过去一个新方案可能要经过十年公开审视；Lazzarin 设想，如果实验室用最强模型和 $20 million 至 $50 million 级别资源持续攻击某一方案而无果，其提供的信心可能超过传统流程十年积累。眼下真正的缺口是实验室掌握了更强模型，却缺少关于其密码攻击测试范围和结果的公开信息。  

4. 量子威胁与 AI 威胁不能混为一谈。量子计算主要威胁 RSA、椭圆曲线等特定体系；AI 若真发现新的高效算法，理论上也可能攻击被视为 post-quantum 的 lattice、hash 或其他候选方案。因此，AI 风险的覆盖面更广，但节目并未提供其已经发生的证据。  

5. 产品和基础设施的现实重点仍是 crypto agility，即系统能在一种假设失效后切换算法。NIST 已在标准化多种公钥体系，部分互联网连接也已同时使用椭圆曲线和 lattice 保护。若椭圆曲线被攻破，迁移到已部署的 post-quantum 方案是已知路径；若多个基础假设同时失效，才需要更漫长的重新标准化过程。  

6. 对区块链而言，hash-based signatures 与 lattice-based schemes 不是简单的“谁更安全”。hash 方案可替换具体 hash function，且能减少区块链依赖的底层假设，但签名更大、使用更复杂，并缺少 threshold signatures、非硬化密钥派生等代数能力。若借助 snark recursion 压缩证明，又会增加实现复杂度与 bug surface。AI 对软件扫描和漏洞发现或许是净正面，但不能因此把复杂系统的正确性视为已解决。  

推荐理由：把“AI 攻破密码学”的情绪性命题拆回可验证假设、迁移机制与真实工程权衡。  
推荐星级：★★★★☆