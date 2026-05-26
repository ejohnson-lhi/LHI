<?php
/**
 * Plugin Name: Lighthouse SMS Signup Webhook Bridge
 * Description: Forwards Fluent Forms /sms-signup/ submissions to the iris-backend droplet so they land in the sms_consent audit log and trigger a confirmation SMS. The shared secret lives in wp-config.php as LHI_SMS_SIGNUP_SECRET so it is never visible in WP admin.
 * Version: 1.0.0
 * Author: Lighthouse Inn
 *
 * INSTALL -- TWO PATHS, pick whichever your host allows:
 *
 *   A) MU-PLUGIN (needs filesystem / SFTP access to wp-content/):
 *      1. Add to wp-config.php above the "stop editing" line:
 *           define('LHI_SMS_SIGNUP_SECRET', 'paste-the-same-secret-as-the-droplet');
 *      2. Upload this file to wp-content/mu-plugins/ (create the folder
 *         if it doesn't exist). mu-plugins auto-load; no activation needed.
 *
 *   B) FLUENTSNIPPETS / CODE SNIPPETS (no filesystem access -- e.g.
 *      GoDaddy Managed WordPress):
 *      1. WP Admin -> Plugins -> Add New -> install + activate
 *         "FluentSnippets" (free, by Fluent Forms' vendor) OR
 *         "Code Snippets" by Code Snippets Pro (free, 600k+ installs).
 *      2. Create a new PHP snippet, run location = "Run snippet
 *         everywhere" (or front-end if that option exists).
 *      3. Paste the contents of this file into the snippet body. If the
 *         snippet plugin complains about the opening <?php tag or the
 *         "Plugin Name:" header comment, remove just those lines -- the
 *         rest of the code is identical.
 *      4. Find the SHARED SECRET block below and uncomment the
 *         define() line, pasting the same secret as the droplet.
 *      5. Activate / Save the snippet.
 *
 *   Either way, confirm LHI_SMS_FORM_ID matches your Fluent Forms form
 *   ID -- visible in Fluent Forms -> All Forms (ID column) or in the
 *   shortcode you embedded, e.g. [fluentform id="3"] -> 3.
 *
 * TEST:
 *   - Submit the form once with a real number.
 *   - On the droplet, run:
 *       sqlite3 /opt/iris-backend/backend/data/lighthouse.db \
 *         "SELECT id, phone_e164, source, client_ip FROM sms_consent
 *          ORDER BY id DESC LIMIT 3;"
 *   - The new row should have source=web_form_signup and your real IP
 *     (not the WP server's IP) in client_ip.
 *   - PHP error log will show a line beginning with [lhi-sms].
 */

if (!defined('ABSPATH')) {
    exit; // Direct access denied.
}

// ─────────────────────────────────────────────────────────────────────
// SHARED SECRET -- snippet-path users uncomment the next line and
// paste the same secret you put on the droplet's .env. mu-plugin-path
// users leave it commented and define LHI_SMS_SIGNUP_SECRET in
// wp-config.php instead. Either way the constant must be defined
// before the submission handler fires, or this code logs a warning
// and skips forwarding.
// ─────────────────────────────────────────────────────────────────────

// define('LHI_SMS_SIGNUP_SECRET', 'paste-the-secret-here');

// ─────────────────────────────────────────────────────────────────────
// Configuration -- update these if anything moves.
// ─────────────────────────────────────────────────────────────────────

// Fluent Forms form ID for the SMS Notifications signup form.
// Matches the shortcode [fluentform id="3"] embedded on /sms-signup/.
const LHI_SMS_FORM_ID = 3;

// Droplet endpoint that accepts the forwarded payload (FastAPI).
const LHI_SMS_WEBHOOK_URL = 'https://iris.lighthouseinn-florence.com/sms-signup/webhook';

// Verbatim consent disclosure the guest agreed to. Sent to the droplet
// so the audit row stores the actual text, not just a version tag.
// Bump LHI_SMS_CONSENT_VERSION below whenever you change this string.
const LHI_SMS_CONSENT_TEXT = 'I agree to receive transactional SMS from The Lighthouse Inn related to my reservation - door code on arrival, checkout reminder, optional payment link, optional cancellation confirmation. Message frequency: 1-5 per stay. Msg & data rates may apply. Reply STOP to opt out, HELP for help. See our Privacy Policy and Terms.';

// Bump whenever LHI_SMS_CONSENT_TEXT changes so we can prove which
// version each historical guest agreed to.
const LHI_SMS_CONSENT_VERSION = 'v1_2026-05-25';

// ─────────────────────────────────────────────────────────────────────
// Submission handler
// ─────────────────────────────────────────────────────────────────────

// Register on both hook names; Fluent Forms renamed it (slash -> underscore)
// between major versions. The unused one is silently ignored.
add_action('fluentform/submission_inserted', 'lhi_sms_forward_submission', 10, 3);
add_action('fluentform_submission_inserted', 'lhi_sms_forward_submission', 10, 3);

function lhi_sms_forward_submission($entry_id, $form_data, $form) {
    // Filter: only forward submissions for the SMS signup form. $form is
    // an object in current FF; older versions may pass the ID directly.
    $form_id = is_object($form) ? (int) $form->id : (int) $form;
    if ($form_id !== LHI_SMS_FORM_ID) {
        return;
    }

    // Secret must be defined in wp-config.php. Without it, we can't auth
    // to the droplet -- log and skip rather than firing an unauthed call.
    if (!defined('LHI_SMS_SIGNUP_SECRET') || !LHI_SMS_SIGNUP_SECRET) {
        error_log('[lhi-sms] Skip entry #' . $entry_id . ': LHI_SMS_SIGNUP_SECRET not defined in wp-config.php');
        return;
    }

    // Pull form fields with null-coalesce so a renamed/removed field
    // doesn't crash the hook -- the droplet will 4xx and log loudly,
    // which is the right signal.
    $name = isset($form_data['name']) ? trim((string) $form_data['name']) : '';
    $reservation_number = isset($form_data['reservation_number']) ? trim((string) $form_data['reservation_number']) : '';
    $mobile = isset($form_data['mobile']) ? trim((string) $form_data['mobile']) : '';
    $consent = $form_data['consent'] ?? '';

    // Some checkbox/GDPR fields submit as arrays -- reduce to a string the
    // droplet's truthy check can recognise.
    if (is_array($consent)) {
        $consent = implode(', ', $consent);
    }

    // Capture the guest's real IP and UA. Behind GoDaddy's edge / any
    // reverse proxy, REMOTE_ADDR is the proxy itself -- the real client
    // IP is the leftmost entry of X-Forwarded-For.
    $ip = '';
    if (!empty($_SERVER['HTTP_X_FORWARDED_FOR'])) {
        $parts = explode(',', $_SERVER['HTTP_X_FORWARDED_FOR']);
        $ip = trim($parts[0]);
    } elseif (!empty($_SERVER['REMOTE_ADDR'])) {
        $ip = $_SERVER['REMOTE_ADDR'];
    }
    $ua = isset($_SERVER['HTTP_USER_AGENT']) ? substr((string) $_SERVER['HTTP_USER_AGENT'], 0, 500) : '';

    $payload = [
        'name' => $name,
        'reservation_number' => $reservation_number,
        'mobile' => $mobile,
        'consent' => $consent,
        'consent_text' => LHI_SMS_CONSENT_TEXT,
        'consent_text_version' => LHI_SMS_CONSENT_VERSION,
        'submitter_ip' => $ip,
        'user_agent' => $ua,
    ];

    $response = wp_remote_post(LHI_SMS_WEBHOOK_URL, [
        'timeout' => 15,
        'headers' => [
            'Content-Type'    => 'application/json',
            'X-Signup-Secret' => LHI_SMS_SIGNUP_SECRET,
        ],
        'body' => wp_json_encode($payload),
    ]);

    if (is_wp_error($response)) {
        error_log('[lhi-sms] Forward failed for entry #' . $entry_id . ': ' . $response->get_error_message());
        return;
    }

    $code = (int) wp_remote_retrieve_response_code($response);
    $body = (string) wp_remote_retrieve_body($response);

    if ($code >= 400) {
        error_log('[lhi-sms] Droplet returned HTTP ' . $code . ' for entry #' . $entry_id . ': ' . substr($body, 0, 500));
    } else {
        error_log('[lhi-sms] Forwarded entry #' . $entry_id . ' OK: ' . substr($body, 0, 200));
    }
}
