
# ETHICS.md

```markdown
# Ethical Guidelines for OSINT Social Media Scraper

## Data Sourcing Boundaries

This project strictly adheres to collecting only publicly available information from social media platforms. We maintain the following boundaries:

1. **Public Data Only**: The scraper only collects data that is publicly visible without authentication. No private or restricted content is accessed.

2. **No Authentication Bypass**: The application does not attempt to bypass authentication mechanisms or access non-public content.

3. **Respect Platform Restrictions**: The scraper respects robots.txt files and platform-specific restrictions on data collection.

4. **Minimal Data Collection**: Only the necessary data specified in the project requirements is collected, avoiding unnecessary information gathering.

## Terms of Service Compliance Strategies

To ensure compliance with platform Terms of Service:

1. **Rate Limiting**: The application implements strict rate limiting (3 requests/minute for X, 2 requests/minute for Facebook) to avoid excessive requests that could violate platform policies.

2. **User-Agent Rotation**: The scraper rotates through a list of common browser user agents to mimic regular browser behavior and avoid detection as a bot.

3. **Respectful Crawling**: The crawler operates during off-peak hours when possible to minimize impact on platform performance.

4. **Data Usage Limitation**: Collected data is used only for the purposes specified in the project requirements and not for commercial exploitation.

5. **Platform-Specific Compliance**: 
   - For X (Twitter): The scraper primarily uses the official Twitter API when available, falling back to scraping only when necessary.
   - For Facebook: The scraper uses the facebook-scraper library which is designed to comply with Facebook's ToS for public data access.

## Automated Privacy Filters

The application includes several automated privacy filters:

1. **Public Profile Verification**: The scraper verifies that profiles are public before attempting data collection.

2. **Age-Gated Content Handling**: The system automatically detects and skips age-gated content on Facebook.

3. **Data Exclusion**: The scraper excludes any private information that might inadvertently be accessible, such as:
   - Private contact information
   - Private messages
   - Non-public friend lists
   - Location data beyond what is publicly shared

4. **Data Retention Policy**: All scraped data is automatically deleted after 30 days in compliance with GDPR requirements.

## Legal Compliance

1. **GDPR Compliance**:
   - Data retention is limited to 30 days
   - Users can request deletion of their data
   - Data processing is limited to what is necessary for the project

2. **CCPA Compliance**:
   - The scraper does not sell personal information
   - California residents can request information about data collected

3. **Platform-Specific Legal Compliance**:
   - Compliance with Twitter's Developer Agreement and Policy
   - Compliance with Facebook's Platform Policy
   - Adherence to all applicable laws regarding data collection and processing

4. **Transparency**:
   - All API responses include an `X-OSINT-Warning: public-data-only` header
   - Clear documentation of data sources and methods

## Ethical Considerations

1. **Purpose Limitation**: The scraper is designed specifically for the educational purpose of demonstrating OSINT techniques and is not intended for malicious use.

2. **Minimization**: Only the minimum necessary data is collected to fulfill the project requirements.

3. **Accuracy**: The application includes validation mechanisms to ensure data accuracy and includes error handling for cases where data cannot be reliably collected.

4. **Accountability**: The project includes comprehensive logging to track all data collection activities for auditing purposes.

5. **Non-Discrimination**: The scraper does not target specific individuals or groups based on protected characteristics.

## Responsible Disclosure

1. **Vulnerability Reporting**: If security vulnerabilities are discovered in the scraping techniques, they will be responsibly disclosed to the affected platforms.

2. **Ethical Review**: The project has undergone an ethical review to ensure it aligns with established OSINT ethics guidelines.

3. **Continuous Improvement**: The ethical guidelines will be reviewed and updated as needed to address new ethical considerations that may arise.

## User Responsibility

Users of this software are responsible for:

1. Using the software in compliance with all applicable laws and regulations
2. Respecting the terms of service of the platforms being scraped
3. Using the collected data ethically and responsibly
4. Complying with data protection regulations when processing or storing collected data

By using this software, you agree to abide by these ethical guidelines and use the software responsibly and legally.