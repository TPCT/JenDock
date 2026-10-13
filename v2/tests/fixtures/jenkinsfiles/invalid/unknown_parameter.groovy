// expect: line 8: unknown parameter 'params.OTHER'
pipeline {
    agent any
    parameters { string(name: 'KNOWN', defaultValue: 'x') }
    stages {
        stage('A') {
            steps {
                echo "${params.OTHER}"
            }
        }
    }
}
