-- remove the CyberSource credentials on neutralized databases
UPDATE payment_provider
   SET mdv_cs_secret_key = NULL,
       mdv_cs_webhook_secret = NULL;
