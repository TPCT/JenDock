pipeline {
    agent none
    stages {
        stage('Build') {
            steps {
                dockerBuild(image: 'aic-website-backend', dockerfile: 'backend/Dockerfile', context: 'backend')
                dockerBuild(image: 'aic-website-frontend', dockerfile: 'frontend/Dockerfile', context: 'frontend', target: 'runner', args: [NEXT_PUBLIC_SITE_URL: 'https://aicconsultation.com', NEXT_PUBLIC_CMS_API_URL: 'https://api.aicconsultation.com'])
                dockerBuild(image: 'aic-website-reverb', dockerfile: 'reverb/Dockerfile', context: 'reverb')
            }
        }
        stage('Test aic-website-backend') {
            agent {
                docker {
                    image 'aic-website-backend'
                }
            }
            environment {
                DATABASE_URL = 'sqlite:///:memory:'
                JWT_SECRET = 'isolated-build-check-no-production-secret'
                MEDIA_ROOT = '/tmp/media'
            }
            steps {
                sh 'python -c \'from app.main import app; schema=app.openapi(); assert schema[\'"\'"\'paths\'"\'"\']; assert any(\'"\'"\'health\'"\'"\' in p for p in schema[\'"\'"\'paths\'"\'"\'])\''
            }
        }
        stage('Test aic-website-reverb') {
            agent {
                docker {
                    image 'aic-website-reverb'
                }
            }
            environment {
                APP_ENV = 'testing'
                APP_KEY = 'base64:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA='
                CACHE_STORE = 'array'
                CACHE_DRIVER = 'array'
                SESSION_DRIVER = 'array'
                QUEUE_CONNECTION = 'sync'
                MAIL_MAILER = 'array'
                DB_CONNECTION = 'sqlite'
                DB_DATABASE = ':memory:'
            }
            steps {
                sh 'mkdir -p storage/framework/cache/data storage/framework/views storage/framework/sessions storage/logs && find app -name \'*.php\' -print0 | xargs -0 -n1 php -l >/dev/null && php artisan route:list --json >/dev/null'
            }
        }
        stage('Deploy') {
            steps {
                deploy(services: ['website-api': 'aic-website-backend', 'website-frontend': 'aic-website-frontend', 'website-reverb': 'aic-website-reverb', 'website-notifications': 'aic-website-backend'])
                healthCheck(url: 'https://aicconsultation.com/', expectStatus: 200)
            }
        }
    }
}
