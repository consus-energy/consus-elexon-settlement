# The EMS boundary, part two: telling them what happened to a flow.
#
# THE MIRROR OF ems_topic.tf, POINTING THE OTHER WAY, and the asymmetry in
# this file is the point. Intents arrive on a topic WE own, because we receive
# on it. Flow events go to a topic the EMS owns, because they receive on it.
# Each project owns the topic it reads, and the only thing that crosses the
# boundary in either direction is one IAM grant.
#
# So there is no topic resource here. There is the service account that
# publishes, the environment variable that names the far topic, and a note
# saying where the grant lives -- because the grant is in the EMS's terraform
# and a reader of this file would otherwise go looking for it here.
#
# WHAT THE EMS DOES WITH IT. Their gate refuses to move a battery on a
# settlement period it cannot show was notified. Deviation Volume is
# calculated only for a period with a BOA or a WMAN (BSC Section T 4.3.AA.1),
# so a battery moved on an unnotified period deviates against nothing and the
# trade it was delivering settles unhedged. Until this channel exists they can
# only read their OWN published intent, which records what they asked for.
#
# UNSET IS OFF, AND THAT IS A REAL STATE. With no topic configured the gateway
# runs exactly as it does today and logs once, at startup, that the EMS will
# hear nothing. Their gate fails SAFE without the events -- no evidence means
# no dispatch -- so the degraded mode is a fleet that does not move, not one
# that moves wrongly.

variable "ems_flow_topic" {
  description = <<-EOT
    The EMS's flow-event topic, as a FULL path:
    "projects/<ems-project>/topics/<name>".

    A full path rather than a project and a name assembled here: a topic in
    the wrong project is a settlement message sent to somebody else's system,
    and one string is one thing to get right instead of two.

    Empty disables the return channel. That is the state until the EMS stands
    its topic up, and the gateway starts and runs normally without it.

    THE GRANT LIVES IN THE EMS'S TERRAFORM, not here. It is
    roles/pubsub.publisher on that topic for
    google_service_account.gateway.email, which this file outputs so the other
    side can name it without anybody reading it out of a console.
  EOT
  type        = string
  default     = ""
}

output "gateway_service_account" {
  description = <<-EOT
    The principal that publishes flow events to the EMS.

    Output so the EMS's terraform can grant it pubsub.publisher on their
    topic. The reverse grant -- their publisher on our intents topic -- is
    var.ems_publishers in ems_topic.tf, and the two together are the whole of
    the cross-project access in either direction.
  EOT
  value       = "serviceAccount:${google_service_account.gateway.email}"
}
