"""B9/B11 の偽の部品。FakePool（worker-pool の POST /v1/jobs の偽。モデルは b0 の FakeModelGateway）、World（本物の timeline・control・ledger を
同じ網に載せた組立。故障は tests/fakes/fault の FaultNet で注入する）。"""
