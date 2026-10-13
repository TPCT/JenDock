pipeline {
    agent { docker { image 'node:22' } }
    options { retry(2) }
    stages {
        stage('Blocks') {
            options {
                timeout(time: 10, unit: 'SECONDS')
            }
            steps {
                withEnv(['CI=true', 'NODE_ENV=test']) {
                    dir('web/app') {
                        timeout(time: 5, unit: 'MINUTES') {
                            retry(3) {
                                sh "echo ${NODE_ENV} $CI"
                            }
                        }
                    }
                }
                timeout(time: 1) {
                    echo 'implicit minutes'
                }
            }
        }
    }
}
