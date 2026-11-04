"""A checked IK rejection is recoverable; transport/identity faults are not."""


class IKRejected(ValueError):
    def __init__(self, response):
        self.response = response
        super().__init__('ik_rejected')
