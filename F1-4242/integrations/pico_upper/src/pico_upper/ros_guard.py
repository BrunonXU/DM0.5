"""Allow-listed ROS output boundary shared by the two Pico processes."""
from rclpy.node import Node
from pico_upper.contract import PUBLISH_TOPICS, validate_message

class CheckedPublisher:
    def __init__(self, publisher, topic):
        self.publisher, self.topic = publisher, topic
    def publish(self, message):
        validate_message(self.topic, message)
        self.publisher.publish(message)

class GuardedNode(Node):
    def create_publisher(self, msg_type, topic, qos_profile, **kwargs):
        # rclpy Node initialization may create its parameter event publisher.
        # These metadata topics cannot command robot actuators.
        if topic in {'/parameter_events', '/rosout'}:
            return super().create_publisher(msg_type, topic, qos_profile, **kwargs)
        if topic not in PUBLISH_TOPICS:
            raise RuntimeError(f'UPPER_ONLY_TOPIC_DENIED {topic}')
        return CheckedPublisher(super().create_publisher(msg_type, topic, qos_profile, **kwargs), topic)
    def create_client(self, *args, **kwargs):
        raise RuntimeError('UPPER_ONLY_SERVICE_CLIENTS_DISABLED')
